from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import cached_property, wraps
from typing import Any, Callable, Generic, Iterable, Literal, TypeVar, cast

import torch
from loguru import logger
from torch import Tensor, device as Device, dtype as DType, nn
from torch.autograd import backward
from torch.optim.lr_scheduler import (
    CosineAnnealingLR,
    CosineAnnealingWarmRestarts,
    CyclicLR,
    ExponentialLR,
    LambdaLR,
    LRScheduler,
    MultiplicativeLR,
    MultiStepLR,
    OneCycleLR,
    ReduceLROnPlateau,
    StepLR,
)
from torch.optim.optimizer import Optimizer

from refiners.fluxion import layers as fl
from refiners.training_utils.callback import (
    Callback,
    CallbackConfig,
)
from refiners.training_utils.clock import ClockConfig, TrainingClock
from refiners.training_utils.common import (
    Step,
    compute_grad_norm,
    count_learnable_parameters,
    human_readable_number,
    scoped_seed,
)
from refiners.training_utils.config import BaseConfig, LRSchedulerType, ModelConfig


class WarmupScheduler(LRScheduler):
    _step_count: int  # defined by LRScheduler

    def __init__(self, optimizer: Optimizer, scheduler: LRScheduler, warmup_scheduler_steps: int = 0) -> None:
        self.warmup_scheduler_steps = warmup_scheduler_steps
        self.scheduler = scheduler
        super().__init__(optimizer=optimizer)

    def get_lr(self) -> list[float] | float:  # type: ignore
        if self._step_count <= self.warmup_scheduler_steps:
            return [base_lr * self._step_count / self.warmup_scheduler_steps for base_lr in self.base_lrs]
        return self.scheduler.get_lr()

    def step(self, epoch: int | None = None) -> None:
        if self._step_count < self.warmup_scheduler_steps:
            super().step()
        else:
            self.scheduler.step(epoch=epoch)
            self._step_count += 1


Batch = TypeVar("Batch")
ConfigType = TypeVar("ConfigType", bound=BaseConfig)


@dataclass
class ModelItem:
    name: str
    config: ModelConfig
    model: fl.Module
    learnable_parameters: list[nn.Parameter]


ModelRegistry = dict[str, ModelItem]
ModuleT = TypeVar("ModuleT", bound=fl.Module)
ModelConfigT = TypeVar("ModelConfigT", bound=ModelConfig)


def register_model():
    def decorator(func: Callable[[Any, ModelConfigT], ModuleT]) -> ModuleT:
        @wraps(func)
        def wrapper(self: Trainer[BaseConfig, Any], config: ModelConfigT) -> fl.Module:
            name = func.__name__
            model = func(self, config)
            model = model.to(self.device, dtype=self.dtype)
            if config.requires_grad is not None:
                logger.info(f"Setting requires_grad to {config.requires_grad} for model: {name}")
                model.requires_grad_(requires_grad=config.requires_grad)
            learnable_parameters = [param for param in model.parameters() if param.requires_grad]
            numel = sum(param.numel() for param in learnable_parameters)
            logger.info(f"Number of learnable parameters in {name}: {human_readable_number(numel)}")
            self.models[name] = ModelItem(
                name=name, config=config, model=model, learnable_parameters=learnable_parameters
            )
            setattr(self, name, self.models[name].model)
            return model

        return wrapper  # type: ignore

    return decorator


CallbackRegistry = dict[str, Callback[Any]]
CallbackT = TypeVar("CallbackT", bound=Callback[Any])
CallbackConfigT = TypeVar("CallbackConfigT", bound=CallbackConfig)


def register_callback():
    def decorator(func: Callable[[Any, CallbackConfigT], CallbackT]) -> CallbackT:
        @wraps(func)
        def wrapper(self: "Trainer[BaseConfig, Any]", config: CallbackConfigT) -> CallbackT:
            name = func.__name__
            callback = func(self, config)
            self.callbacks[name] = callback
            setattr(self, name, callback)
            return callback

        return wrapper  # type: ignore

    return decorator


class Trainer(Generic[ConfigType, Batch], ABC):
    def __init__(self, config: ConfigType) -> None:
        self._models: ModelRegistry = {}
        self._callbacks: CallbackRegistry = {}
        self.config = config
        self._load_callbacks()
        self._call_callbacks(event_name="on_init_begin")
        self._load_models()
        self._call_callbacks(event_name="on_init_end")

        # Ensure the lr_scheduler is initialized before calling `step` on the optimizer.
        # See `patch_track_step_called` in LRScheduler constructor.
        assert self.lr_scheduler

    @register_callback()
    def clock(self, config: ClockConfig) -> TrainingClock:
        return TrainingClock(
            training_duration=self.config.training.duration,
            gradient_accumulation=self.config.training.gradient_accumulation,
            verbose=config.verbose,
        )

    @property
    def models(self) -> ModelRegistry:
        return self._models

    @property
    def callbacks(self) -> CallbackRegistry:
        return self._callbacks

    @cached_property
    def device(self) -> Device:
        selected_device = Device(self.config.training.device)
        logger.info(f"Using device: {selected_device}")
        return selected_device

    @cached_property
    def dtype(self) -> DType:
        dtype = getattr(torch, self.config.training.dtype, None)
        assert isinstance(dtype, DType), f"Unknown dtype: {self.config.training.dtype}"
        logger.info(f"Using dtype: {dtype}")
        return dtype

    @property
    def learnable_parameters(self) -> list[nn.Parameter]:
        """Returns a list of learnable parameters in all models"""
        return [param for item in self.models.values() for param in item.learnable_parameters]

    @cached_property
    def optimizer_parameters(self) -> list[dict[str, Any]]:
        """
        Returns a list of `dict`-s containing the params and optimizer options for each model.
        See https://pytorch.org/docs/stable/optim.html#per-parameter-options for more details
        """
        params: list[dict[str, Any]] = []
        for item in self.models.values():
            config = item.config
            model_optim_conf: dict[str, Any] = {}

            if config.learning_rate is not None:
                model_optim_conf["lr"] = config.learning_rate
            if config.weight_decay is not None:
                model_optim_conf["weight_decay"] = config.learning_rate
            if config.betas is not None:
                model_optim_conf["betas"] = config.learning_rate
            if config.eps is not None:
                model_optim_conf["eps"] = config.learning_rate

            params.append({"params": item.learnable_parameters, **model_optim_conf})

        return params

    @property
    def learnable_parameter_count(self) -> int:
        """Returns the number of learnable parameters in all models"""
        return count_learnable_parameters(parameters=self.learnable_parameters)

    @property
    def gradients(self) -> list[Tensor]:
        """Returns a list of detached gradients for all learnable parameters in all models"""
        return [param.grad.detach() for param in self.learnable_parameters if param.grad is not None]

    @property
    def total_gradient_norm(self) -> float:
        """Returns the total gradient norm for all learnable parameters in all models"""
        return compute_grad_norm(parameters=self.learnable_parameters)

    @cached_property
    def optimizer(self) -> Optimizer:
        formatted_param_count = human_readable_number(number=self.learnable_parameter_count)
        logger.info(f"Total number of learnable parameters in the model(s): {formatted_param_count}")

        optimizer = self.config.optimizer.get(params=self.optimizer_parameters)
        return optimizer

    @cached_property
    def lr_scheduler(self) -> LRScheduler:
        config = self.config.lr_scheduler
        scheduler_step_size = config.update_interval.number

        match config.type:
            case LRSchedulerType.CONSTANT_LR:
                lr_scheduler = LambdaLR(optimizer=self.optimizer, lr_lambda=lambda _: 1.0)
            case LRSchedulerType.STEP_LR:
                lr_scheduler = StepLR(optimizer=self.optimizer, step_size=scheduler_step_size, gamma=config.gamma)
            case LRSchedulerType.EXPONENTIAL_LR:
                lr_scheduler = ExponentialLR(optimizer=self.optimizer, gamma=config.gamma)
            case LRSchedulerType.COSINE_ANNEALING_LR:
                lr_scheduler = CosineAnnealingLR(
                    optimizer=self.optimizer,
                    T_max=scheduler_step_size,
                    eta_min=config.eta_min,  # pyright: ignore[reportArgumentType]
                )
            case LRSchedulerType.REDUCE_LR_ON_PLATEAU:
                lr_scheduler = cast(
                    LRScheduler,
                    ReduceLROnPlateau(
                        optimizer=self.optimizer,
                        mode=config.mode,
                        factor=config.factor,
                        patience=config.patience,
                        threshold=config.threshold,
                        cooldown=config.cooldown,
                        min_lr=config.min_lr,
                    ),
                )
            case LRSchedulerType.LAMBDA_LR:
                assert config.lr_lambda is not None, "lr_lambda must be specified to use LambdaLR"
                lr_scheduler = LambdaLR(optimizer=self.optimizer, lr_lambda=config.lr_lambda)
            case LRSchedulerType.ONE_CYCLE_LR:
                lr_scheduler = OneCycleLR(
                    optimizer=self.optimizer, max_lr=config.max_lr, total_steps=scheduler_step_size
                )
            case LRSchedulerType.MULTIPLICATIVE_LR:
                assert config.lr_lambda is not None, "lr_lambda must be specified to use MultiplicativeLR"
                lr_scheduler = MultiplicativeLR(optimizer=self.optimizer, lr_lambda=config.lr_lambda)
            case LRSchedulerType.COSINE_ANNEALING_WARM_RESTARTS:
                lr_scheduler = CosineAnnealingWarmRestarts(optimizer=self.optimizer, T_0=scheduler_step_size)
            case LRSchedulerType.CYCLIC_LR:
                lr_scheduler = CyclicLR(optimizer=self.optimizer, base_lr=config.base_lr, max_lr=config.max_lr)
            case LRSchedulerType.MULTI_STEP_LR:
                lr_scheduler = MultiStepLR(optimizer=self.optimizer, milestones=config.milestones, gamma=config.gamma)
            case _:
                raise ValueError(f"Unknown scheduler type: {config.type}")

        warmup_scheduler_steps = (
            config.warmup.number
            if isinstance(config.warmup, Step)
            else config.warmup.number * self.clock.gradient_accumulation.number
        )
        if warmup_scheduler_steps > 0:
            lr_scheduler = WarmupScheduler(
                optimizer=self.optimizer,
                scheduler=lr_scheduler,
                warmup_scheduler_steps=warmup_scheduler_steps,
            )

        return lr_scheduler

    @abstractmethod
    def compute_loss(self, batch: Batch) -> Tensor: ...

    @abstractmethod
    def create_data_iterable(self) -> Iterable[Batch]: ...

    @cached_property
    def data_iterable(self) -> Iterable[Batch]:
        return self.create_data_iterable()

    def backward(self) -> None:
        """Backward pass on the loss."""
        self._call_callbacks(event_name="on_backward_begin")
        scaled_loss = self.loss / self.config.training.gradient_accumulation.number
        backward(tensors=scaled_loss)
        self._call_callbacks(event_name="on_backward_end")
        if self.clock.is_optimizer_step:
            self._call_callbacks(event_name="on_optimizer_step_begin")
            max_norm = self.config.training.gradient_clipping_max_norm or float("inf")
            self.grad_norm = nn.utils.clip_grad.clip_grad_norm_(self.learnable_parameters, max_norm=max_norm).item()
            self.optimizer.step()
            self.optimizer.zero_grad()
            self._call_callbacks(event_name="on_optimizer_step_end")
            if self.clock.is_due(self.config.lr_scheduler.update_interval):
                # TODO: if the update interval is in Epochs, this will be called
                # at every optimizer step during targeted epochs. It should probably
                # only be called once instead.
                self._call_callbacks(event_name="on_lr_scheduler_step_begin")
                self.lr_scheduler.step()
                self._call_callbacks(event_name="on_lr_scheduler_step_end")

    def step(self, batch: Batch) -> None:
        """Perform a single training step."""
        self._call_callbacks(event_name="on_compute_loss_begin")
        loss = self.compute_loss(batch=batch)
        self.loss = loss
        self._call_callbacks(event_name="on_compute_loss_end")
        self.backward()

    def epoch(self) -> None:
        """Perform a single epoch."""
        for batch in self.data_iterable:
            if self.clock.done:
                break
            self._call_callbacks(event_name="on_step_begin")
            self.step(batch=batch)
            self._call_callbacks(event_name="on_step_end")

    @staticmethod
    def get_training_seed(instance: "Trainer[BaseConfig, Any]") -> int:
        return instance.config.training.seed

    @scoped_seed(seed=get_training_seed)
    def train(self) -> None:
        """Train the model."""
        self.set_models_to_mode("train")
        self._call_callbacks(event_name="on_train_begin")
        assert self.learnable_parameters, "There are no learnable parameters in the models."
        while not self.clock.done:
            self._call_callbacks(event_name="on_epoch_begin")
            self.epoch()
            self._call_callbacks(event_name="on_epoch_end")
        self._call_callbacks(event_name="on_train_end")

    def set_models_to_mode(self, mode: Literal["train", "eval"]) -> None:
        for item in self.models.values():
            if mode == "train":
                item.model.train()
            elif mode == "eval":
                item.model.eval()

    def _run_event(self, callback: Callback[Any], event_name: str) -> None:
        getattr(callback, event_name)(self)

    def _call_callbacks(self, event_name: str) -> None:
        if event_name.endswith("_begin"):
            self._run_event(self.clock, event_name)

        for callback in self.callbacks.values():
            if callback == self.clock:
                continue
            self._run_event(callback, event_name)

        if event_name.endswith("_end"):
            self._run_event(self.clock, event_name)

    def _load_callbacks(self) -> None:
        for name, config in self.config:
            if not isinstance(config, CallbackConfig):
                continue
            try:
                registered_callback = getattr(self, name)
            except AttributeError:
                raise ValueError(
                    f"Callback {name} is in the config but not registered in the Trainer. Create a method with the @register_callback decorator."
                )
            assert callable(registered_callback)
            registered_callback(config)

    def _load_models(self) -> None:
        for name, config in self.config:
            if not isinstance(config, ModelConfig):
                continue
            try:
                registered_model = getattr(self, name)
            except AttributeError:
                raise ValueError(
                    f"Model {name} is in the config but not registered in the Trainer. Create a method with the @register_model decorator."
                )
            assert callable(registered_model)
            registered_model(config)
