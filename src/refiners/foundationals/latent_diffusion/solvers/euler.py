import numpy as np
import torch
from torch import Generator, Tensor, device as Device, dtype as Dtype, float32, tensor

from refiners.foundationals.latent_diffusion.solvers.solver import NoiseSchedule, Solver


class Euler(Solver):
    def __init__(
        self,
        num_inference_steps: int,
        num_train_timesteps: int = 1_000,
        initial_diffusion_rate: float = 8.5e-4,
        final_diffusion_rate: float = 1.2e-2,
        noise_schedule: NoiseSchedule = NoiseSchedule.QUADRATIC,
        first_inference_step: int = 0,
        device: Device | str = "cpu",
        dtype: Dtype = float32,
    ):
        if noise_schedule != NoiseSchedule.QUADRATIC:
            raise NotImplementedError
        super().__init__(
            num_inference_steps=num_inference_steps,
            num_train_timesteps=num_train_timesteps,
            initial_diffusion_rate=initial_diffusion_rate,
            final_diffusion_rate=final_diffusion_rate,
            noise_schedule=noise_schedule,
            first_inference_step=first_inference_step,
            device=device,
            dtype=dtype,
        )
        self.sigmas = self._generate_sigmas()

    @property
    def init_noise_sigma(self) -> Tensor:
        return self.sigmas.max()

    def _generate_timesteps(self) -> Tensor:
        # We need to use numpy here because:
        # numpy.linspace(0,999,31)[15] is 499.49999999999994
        # torch.linspace(0,999,31)[15] is 499.5
        # ...and we want the same result as the original codebase.
        timesteps = torch.tensor(np.linspace(0, self.num_train_timesteps - 1, self.num_inference_steps)).flip(0)
        return timesteps

    def _generate_sigmas(self) -> Tensor:
        sigmas = self.noise_std / self.cumulative_scale_factors
        sigmas = torch.tensor(np.interp(self.timesteps.cpu().numpy(), np.arange(0, len(sigmas)), sigmas.cpu().numpy()))
        sigmas = torch.cat([sigmas, tensor([0.0])])
        return sigmas.to(device=self.device, dtype=self.dtype)

    def scale_model_input(self, x: Tensor, step: int) -> Tensor:
        sigma = self.sigmas[step]
        return x / ((sigma**2 + 1) ** 0.5)

    def __call__(
        self,
        x: Tensor,
        predicted_noise: Tensor,
        step: int,
        generator: Generator | None = None,
    ) -> Tensor:
        assert self.first_inference_step <= step < self.num_inference_steps, "invalid step {step}"
        return x + predicted_noise * (self.sigmas[step + 1] - self.sigmas[step])