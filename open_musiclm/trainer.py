import itertools
from dataclasses import dataclass
from pathlib import Path
from shutil import rmtree
import torch
import torch.nn.functional as F
import tqdm
from accelerate import Accelerator, DistributedType
from audiolm_pytorch import FairseqVQWav2Vec, HubertWithKmeans, SoundStream
from audiolm_pytorch.hubert_kmeans import HubertWithKmeans
from audiolm_pytorch.optimizer import get_optimizer
from audiolm_pytorch.t5 import DEFAULT_T5_NAME
from audiolm_pytorch.vq_wav2vec import FairseqVQWav2Vec
from beartype import beartype
from beartype.door import is_bearable
from beartype.typing import Dict, List, Literal, Optional, Union
from beartype.vale import Is
from einops import rearrange, reduce, repeat
from einops.layers.torch import Rearrange
from torch import einsum, nn
from torch.utils.data import DataLoader, Dataset, random_split
from typing_extensions import Annotated

from .clap_quantized import ClapQuantized
from .data import SoundDataset, get_dataloader
from .model_types import NeuralCodec, Wav2Vec
from .open_musiclm import (CoarseStage, FineStage, SemanticStage,
                           TokenConditionedTransformer)
from .utils import (all_rows_have_eos_id, append_eos_id,
                    batch_unique_consecutive, ceil_div, default,
                    eval_decorator, exists, generate_mask_with_prob,
                    get_embeds, gumbel_sample, mask_out_after_eos_id,
                    round_down_nearest_multiple, top_k)

# for automatically routing data emitted from a dataset to keywords of the transformer wrappers

DATASET_FIELD_TYPE_CONFIG = dict(
    input_audio=Annotated[
        torch.Tensor,
        Is[lambda t: t.dtype == torch.float and t.ndim in {2, 3}]
    ],
)


def cycle(dl):
    while True:
        for data in dl:
            yield data


def yes_or_no(question):
    answer = input(f'{question} (y/n) ')
    return answer.lower() in ('yes', 'y')


def accum_log(log, new_logs):
    for key, new_value in new_logs.items():
        old_value = log.get(key, 0.)
        log[key] = old_value + new_value
    return log


# auto data to module keyword argument routing functions

def has_duplicates(tup):
    counts = dict()
    for el in tup:
        if el not in counts:
            counts[el] = 0
        counts[el] += 1
    return any(filter(lambda count: count > 1, counts.values()))


def determine_types(data, config):
    output = []
    for el in data:
        for name, data_type in config.items():
            if is_bearable(el, data_type):
                output.append(name)
                break
        else:
            raise TypeError(f'unable to determine type of {data}')

    return tuple(output)


def noop(*args, **kwargs):
    pass


@beartype
class SingleStageTrainer(nn.Module):
    """General trainer for any stage"""

    def __init__(
        self,
        transformer: TokenConditionedTransformer,
        stage: Literal['semantic', 'coarse', 'fine'],
        *,
        num_train_steps,
        batch_size,
        dataset: Optional[Dataset] = None,
        wav2vec: Optional[Wav2Vec] = None,
        neural_codec: Optional[NeuralCodec] = None,
        audio_conditioner: Optional[ClapQuantized] = None,
        data_max_length=None,
        folder=None,
        lr=3e-4,
        grad_accum_every=1,
        wd=0.,
        max_grad_norm=0.5,
        valid_frac=0.05,
        random_split_seed=42,
        save_results_every=100,
        save_model_every=1000,
        results_folder='./results',
        accelerate_kwargs: dict = {}
    ):
        super().__init__()
        self.accelerator = Accelerator(**accelerate_kwargs)

        self.wav2vec = wav2vec
        self.transformer = transformer
        self.audio_conditioner = audio_conditioner

        if stage == 'semantic':
            assert exists(audio_conditioner) and exists(wav2vec)
            self.train_wrapper = SemanticStage(
                semantic_transformer=transformer,
                wav2vec=wav2vec,
                clap=audio_conditioner,
            )
        elif stage == 'coarse':
            assert exists(wav2vec) and exists(audio_conditioner) and exists(neural_codec)
            self.train_wrapper = CoarseStage(
                coarse_transformer=transformer,
                neural_codec=neural_codec,
                wav2vec=wav2vec,
                clap=audio_conditioner,
                audio_conditioner=audio_conditioner
            )
        elif stage == 'fine':
            assert exists(audio_conditioner) and exists(neural_codec)
            self.train_wrapper = FineStage(
                fine_transformer=transformer,
                clap=audio_conditioner,
                neural_codec=neural_codec,
            )
        else:
            raise ValueError(f'invalid stage: {stage}')

        self.register_buffer('steps', torch.Tensor([0]))

        self.num_train_steps = num_train_steps
        self.batch_size = batch_size
        self.grad_accum_every = grad_accum_every

        # optimizers

        self.optim = get_optimizer(transformer.parameters(), lr=lr, wd=wd)

        # max grad norm

        self.max_grad_norm = max_grad_norm

        # create dataset

        self.ds = dataset
        if not exists(self.ds):
            assert exists(
                folder), 'folder must be passed in, if not passing in a custom dataset for text conditioned audio synthesis training'

            self.ds = SoundDataset(
                folder,
                max_length=data_max_length,
                target_sample_hz=wav2vec.target_sample_hz,
                seq_len_multiple_of=wav2vec.seq_len_multiple_of
            )

        self.ds_fields = None

        # split for validation

        if valid_frac > 0:
            train_size = int((1 - valid_frac) * len(self.ds))
            valid_size = len(self.ds) - train_size
            self.ds, self.valid_ds = random_split(
                self.ds, [train_size, valid_size], generator=torch.Generator().manual_seed(random_split_seed))
            self.print(
                f'training with dataset of {len(self.ds)} samples and validating with randomly splitted {len(self.valid_ds)} samples')
        else:
            self.valid_ds = self.ds
            self.print(f'training with shared training and valid dataset of {len(self.ds)} samples')

        # dataloader

        self.dl = get_dataloader(self.ds, batch_size=batch_size, shuffle=True)

        self.valid_dl = get_dataloader(self.valid_ds, batch_size=batch_size, shuffle=True)

        # prepare with accelerator

        (
            self.train_wrapper,
            self.optim,
            self.dl,
            self.valid_dl
        ) = self.accelerator.prepare(
            self.train_wrapper,
            self.optim,
            self.dl,
            self.valid_dl
        )

        # dataloader iterators

        self.dl_iter = cycle(self.dl)
        self.valid_dl_iter = cycle(self.valid_dl)

        self.save_model_every = save_model_every
        self.save_results_every = save_results_every

        self.results_folder = Path(results_folder)

        if len([*self.results_folder.glob('**/*')]) > 0 and yes_or_no('do you want to clear previous experiment checkpoints and results?'):
            rmtree(str(self.results_folder))

        self.results_folder.mkdir(parents=True, exist_ok=True)

        hps = {"num_train_steps": num_train_steps, "data_max_length": data_max_length, "learning_rate": lr}
        self.accelerator.init_trackers("semantic", config=hps)

    def save(self, path):
        pkg = dict(
            model=self.accelerator.get_state_dict(self.transformer),
            optim=self.optim.state_dict()
        )
        torch.save(pkg, path)

    def load(self, path):
        path = Path(path)
        assert path.exists()
        pkg = torch.load(str(path))

        transformer = self.accelerator.unwrap_model(self.transformer)
        transformer.load_state_dict(pkg['model'])
        self.optim.load_state_dict(pkg['optim'])

    def print(self, msg):
        self.accelerator.print(msg)

    def generate(self, *args, **kwargs):
        return self.train_wrapper.generate(*args, **kwargs)

    @property
    def device(self):
        return self.accelerator.device

    @property
    def is_distributed(self):
        return not (self.accelerator.distributed_type == DistributedType.NO and self.accelerator.num_processes == 1)

    @property
    def is_main(self):
        return self.accelerator.is_main_process

    @property
    def is_local_main(self):
        return self.accelerator.is_local_main_process

    def data_tuple_to_kwargs(self, data):
        if not exists(self.ds_fields):
            self.ds_fields = determine_types(data, DATASET_FIELD_TYPE_CONFIG)
            assert not has_duplicates(self.ds_fields), 'dataset fields must not have duplicate field names'

        return dict(zip(self.ds_fields, data))

    def train_step(self):
        device = self.device

        steps = int(self.steps.item())

        self.transformer.train()

        # logs

        logs = {}

        # update vae (generator)

        for _ in range(self.grad_accum_every):
            data_kwargs = self.data_tuple_to_kwargs(next(self.dl_iter))

            loss = self.train_wrapper(**data_kwargs, return_loss=True)

            self.accelerator.backward(loss / self.grad_accum_every)

            accum_log(logs, {'loss': loss.item() / self.grad_accum_every})

        if exists(self.max_grad_norm):
            self.accelerator.clip_grad_norm_(self.transformer.parameters(), self.max_grad_norm)

        self.optim.step()
        self.optim.zero_grad()

        # log

        self.print(f"{steps}: loss: {logs['loss']}")
        self.accelerator.log({"train_loss": logs['loss']}, step=steps)

        # sample results every so often

        if self.is_main and not (steps % self.save_results_every):
            data_kwargs = self.data_tuple_to_kwargs(next(self.valid_dl_iter))

            with torch.no_grad():
                self.train_wrapper.eval()
                valid_loss = self.train_wrapper(**data_kwargs, return_loss=True)

            self.print(f'{steps}: valid loss {valid_loss}')
            self.accelerator.log({"valid_loss": valid_loss}, step=steps)

        # save model every so often

        if self.is_main and not (steps % self.save_model_every):
            state_dict = self.transformer.state_dict()
            model_path = str(self.results_folder / f'semantic.transformer.{steps}.pt')
            torch.save(state_dict, model_path)

            self.print(f'{steps}: saving model to {str(self.results_folder)}')

        self.steps += 1
        return logs

    def train(self, log_fn=noop):

        while self.steps < self.num_train_steps:
            logs = self.train_step()
            log_fn(logs)

        self.print('training complete')