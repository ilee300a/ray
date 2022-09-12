import inspect
import os
import composer.trainer

from ray.air import session
from ray.train.constants import (
    EVALUATION_DATASET_KEY,
    TRAIN_DATASET_KEY,
)

from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Iterable
from ray.air.checkpoint import Checkpoint
from ray.air.config import DatasetConfig, RunConfig, ScalingConfig
from ray.train.mosaic._mosaic_utils import process_datasets
from ray.train.torch import TorchConfig, TorchTrainer
from ray.train.trainer import GenDataset
from ray.util import PublicAPI

if TYPE_CHECKING:
    from ray.data.preprocessor import Preprocessor


@PublicAPI(stability="alpha")
class MosaicTrainer(TorchTrainer):
    """A Trainer for data parallel Mosaic Composers on PyTorch training.

    This Trainer runs the ``composer.trainer.Trainer.fit()`` method on multiple
    Ray Actors. The training is carried out in a distributed fashion through PyTorch
    DDP. These actors already have the necessary torch process group already
    configured for distributed PyTorch training.

    The training function ran on every Actor will first run the
    specified ``trainer_init_per_worker`` function to obtain an instantiated
    ``composer.Trainer`` object. The ``trainer_init_per_worker`` function
    will have access to preprocessed train and evaluation datasets.

    Args:
        trainer_init_per_worker: The function that returns an instantiated
            ``composer.Trainer`` object and takes in the following arguments:
            train ``Iterable``, optional evaluation
            ``Iterable`` and config as kwargs. The Composer Trainer should take in
            these arguments as ``train_dataloader`` and ``eval_dataloader`` without
            creating a new dataloader for each dataset. The Iterables are automatically
            created by converting the Ray Datasets internally before they are passed
            into the function.
        datasets: Any Ray Datasets to use for training. The datasets must be mapped to
            pandas DataFrame and the labels for each column should be provided. Use
            the key "train" to denote which dataset is the training
            dataset and (optionally) key "evaluation" to denote the evaluation
            dataset. Can only contain a training dataset
            and up to one extra dataset to be used for evaluation.
            If a ``preprocessor`` is provided and has not already been fit,
            it will be fit on the training dataset. All datasets will be
            transformed by the ``preprocessor`` if one is provided.
        trainer_init_config: Configurations to pass into
            ``trainer_init_per_worker`` as kwargs.
        torch_config: Configuration for setting up the PyTorch backend. If set to
            None, use the default configuration. This replaces the ``backend_config``
            arg of ``DataParallelTrainer``. Same as in ``TorchTrainer``.
        scaling_config: Configuration for how to scale data parallel training.
        dataset_config: Configuration for dataset ingest.
        run_config: Configuration for the execution of the training run.
        preprocessor: A ray.data.Preprocessor to preprocess the
            provided datasets.
        resume_from_checkpoint: A checkpoint to resume training from.
    """

    def __init__(
        self,
        trainer_init_per_worker: Callable[
            [Iterable, Optional[Iterable], Any], composer.trainer.Trainer
        ],
        *,
        datasets: Dict[str, GenDataset],
        trainer_init_config: Optional[Dict] = None,
        torch_config: Optional[TorchConfig] = None,
        scaling_config: Optional[ScalingConfig] = None,
        dataset_config: Optional[Dict[str, DatasetConfig]] = None,
        run_config: Optional[RunConfig] = None,
        preprocessor: Optional["Preprocessor"] = None,
        resume_from_checkpoint: Optional[Checkpoint] = None,
    ):

        self._validate_trainer_init_per_worker(
            trainer_init_per_worker, "trainer_init_per_worker"
        )

        trainer_init_config = trainer_init_config.copy() if trainer_init_config else {}
        if "_trainer_init_per_worker" in trainer_init_config:
            raise ValueError(
                "'_trainer_init_per_worker' is a reserved key in `trainer_init_config`."
            )
        trainer_init_config["_trainer_init_per_worker"] = trainer_init_per_worker

        super().__init__(
            train_loop_per_worker=_mosaic_train_loop_per_worker,
            train_loop_config=trainer_init_config,
            torch_config=torch_config,
            scaling_config=scaling_config,
            dataset_config=dataset_config,
            run_config=run_config,
            datasets=datasets,
            preprocessor=preprocessor,
            resume_from_checkpoint=resume_from_checkpoint,
        )

    def _validate_trainer_init_per_worker(
        self, trainer_init_per_worker: Callable, fn_name: str
    ) -> None:
        num_params = len(inspect.signature(trainer_init_per_worker).parameters)
        if num_params < 3:
            raise ValueError(
                f"{fn_name} should take in at least 3 arguments, "
                f"but it accepts {num_params} arguments instead."
            )


def _mosaic_train_loop_per_worker(config):
    """Per-worker training loop for Mosaic Composers."""
    trainer_init_per_worker = config.pop("_trainer_init_per_worker")

    os.environ["RANK"] = str(session.get_world_rank())
    os.environ["WORLD_SIZE"] = str(session.get_world_size())
    os.environ["LOCAL_RANK"] = str(session.get_local_rank())

    # Arbitrary values set for these as they are needed for some composer functions
    os.environ["LOCAL_WORLD_SIZE"] = str(1)
    os.environ["NODE_RANK"] = str(0)

    # get dataset shard
    train_dataset = session.get_dataset_shard(TRAIN_DATASET_KEY)
    eval_dataset = session.get_dataset_shard(EVALUATION_DATASET_KEY)

    train_torch_iterable, eval_torch_iterable = process_datasets(
        train_dataset, eval_dataset, config["batch_size"], config["labels"]
    )

    trainer: composer.trainer.Trainer = trainer_init_per_worker(
        train_torch_iterable, eval_torch_iterable, **config
    )

    # TODO : add supports for logging

    # TODO : add callbacks if needed

    # TODO : add checkpoint if needed

    # call the trainer
    trainer.fit()
