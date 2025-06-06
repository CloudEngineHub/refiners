import gc
from pathlib import Path
from warnings import warn

import pytest
import torch
from PIL import Image
from tests.utils import ensure_similar_images

from refiners.fluxion.utils import load_from_safetensors, manual_seed, no_grad
from refiners.foundationals.latent_diffusion.lora import SDLoraManager
from refiners.foundationals.latent_diffusion.solvers import Euler, ModelPredictionType, SolverParams, TimestepSpacing
from refiners.foundationals.latent_diffusion.stable_diffusion_xl.lcm_lora import add_lcm_lora
from refiners.foundationals.latent_diffusion.stable_diffusion_xl.model import StableDiffusion_XL


@pytest.fixture(autouse=True)
def ensure_gc():
    # Avoid GPU OOMs
    # See https://github.com/pytest-dev/pytest/discussions/8153#discussioncomment-214812
    gc.collect()


@pytest.fixture(scope="module")
def ref_path(test_e2e_path: Path) -> Path:
    return test_e2e_path / "test_lightning_ref"


@pytest.fixture
def expected_lightning_base_4step(ref_path: Path) -> Image.Image:
    return Image.open(ref_path / "expected_lightning_base_4step.png").convert("RGB")


@pytest.fixture
def expected_lightning_base_1step(ref_path: Path) -> Image.Image:
    return Image.open(ref_path / "expected_lightning_base_1step.png").convert("RGB")


@pytest.fixture
def expected_lightning_lora_4step(ref_path: Path) -> Image.Image:
    return Image.open(ref_path / "expected_lightning_lora_4step.png").convert("RGB")


@no_grad()
def test_lightning_base_4step(
    test_device: torch.device,
    sdxl_autoencoder_fp16fix_weights_path: Path,
    sdxl_unet_lightning_4step_weights_path: Path,
    sdxl_text_encoder_weights_path: Path,
    expected_lightning_base_4step: Image.Image,
) -> None:
    if test_device.type == "cpu":
        warn(message="not running on CPU, skipping")
        pytest.skip()

    unet_weights = sdxl_unet_lightning_4step_weights_path
    expected_image = expected_lightning_base_4step

    solver = Euler(
        num_inference_steps=4,
        params=SolverParams(
            timesteps_spacing=TimestepSpacing.TRAILING,
            model_prediction_type=ModelPredictionType.NOISE,
        ),
    )

    sdxl = StableDiffusion_XL(device=test_device, dtype=torch.float16, solver=solver)
    sdxl.classifier_free_guidance = False

    sdxl.clip_text_encoder.load_from_safetensors(sdxl_text_encoder_weights_path)
    sdxl.lda.load_from_safetensors(sdxl_autoencoder_fp16fix_weights_path)
    sdxl.unet.load_from_safetensors(unet_weights)

    prompt = "Self-portrait oil painting, a beautiful cyborg with golden hair, 8k"

    clip_text_embedding, pooled_text_embedding = sdxl.compute_clip_text_embedding(prompt)
    time_ids = sdxl.default_time_ids

    manual_seed(0)
    x = sdxl.init_latents((1024, 1024)).to(sdxl.device, sdxl.dtype)

    for step in sdxl.steps:
        x = sdxl(
            x,
            step=step,
            clip_text_embedding=clip_text_embedding,
            pooled_text_embedding=pooled_text_embedding,
            time_ids=time_ids,
        )
    predicted_image = sdxl.lda.latents_to_image(x)

    ensure_similar_images(predicted_image, expected_image, min_psnr=40, min_ssim=0.98)


@no_grad()
def test_lightning_base_1step(
    test_device: torch.device,
    sdxl_autoencoder_fp16fix_weights_path: Path,
    sdxl_unet_lightning_1step_weights_path: Path,
    sdxl_text_encoder_weights_path: Path,
    expected_lightning_base_1step: Image.Image,
) -> None:
    if test_device.type == "cpu":
        warn(message="not running on CPU, skipping")
        pytest.skip()

    unet_weights = sdxl_unet_lightning_1step_weights_path
    expected_image = expected_lightning_base_1step

    solver = Euler(
        num_inference_steps=1,
        params=SolverParams(
            timesteps_spacing=TimestepSpacing.TRAILING,
            model_prediction_type=ModelPredictionType.SAMPLE,  # 1 step special case
        ),
    )

    sdxl = StableDiffusion_XL(device=test_device, dtype=torch.float16, solver=solver)
    sdxl.classifier_free_guidance = False

    sdxl.clip_text_encoder.load_from_safetensors(sdxl_text_encoder_weights_path)
    sdxl.lda.load_from_safetensors(sdxl_autoencoder_fp16fix_weights_path)
    sdxl.unet.load_from_safetensors(unet_weights)

    prompt = "Self-portrait oil painting, a beautiful cyborg with golden hair, 8k"

    clip_text_embedding, pooled_text_embedding = sdxl.compute_clip_text_embedding(prompt)
    time_ids = sdxl.default_time_ids

    manual_seed(0)
    x = sdxl.init_latents((1024, 1024)).to(sdxl.device, sdxl.dtype)

    for step in sdxl.steps:
        x = sdxl(
            x,
            step=step,
            clip_text_embedding=clip_text_embedding,
            pooled_text_embedding=pooled_text_embedding,
            time_ids=time_ids,
        )
    predicted_image = sdxl.lda.latents_to_image(x)

    ensure_similar_images(predicted_image, expected_image, min_psnr=40, min_ssim=0.98)


@no_grad()
def test_lightning_lora_4step(
    test_device: torch.device,
    sdxl_autoencoder_fp16fix_weights_path: Path,
    sdxl_unet_weights_path: Path,
    sdxl_text_encoder_weights_path: Path,
    lora_sdxl_lightning_4step_weights_path: Path,
    expected_lightning_lora_4step: Image.Image,
) -> None:
    if test_device.type == "cpu":
        warn(message="not running on CPU, skipping")
        pytest.skip()

    expected_image = expected_lightning_lora_4step

    solver = Euler(
        num_inference_steps=4,
        params=SolverParams(
            timesteps_spacing=TimestepSpacing.TRAILING,
            model_prediction_type=ModelPredictionType.NOISE,
        ),
    )

    sdxl = StableDiffusion_XL(device=test_device, dtype=torch.float16, solver=solver)
    sdxl.classifier_free_guidance = False

    sdxl.clip_text_encoder.load_from_safetensors(sdxl_text_encoder_weights_path)
    sdxl.lda.load_from_safetensors(sdxl_autoencoder_fp16fix_weights_path)
    sdxl.unet.load_from_safetensors(sdxl_unet_weights_path)

    manager = SDLoraManager(sdxl)
    add_lcm_lora(manager, load_from_safetensors(lora_sdxl_lightning_4step_weights_path), name="lightning")

    prompt = "Self-portrait oil painting, a beautiful cyborg with golden hair, 8k"

    clip_text_embedding, pooled_text_embedding = sdxl.compute_clip_text_embedding(prompt)
    time_ids = sdxl.default_time_ids

    manual_seed(0)
    x = sdxl.init_latents((1024, 1024)).to(sdxl.device, sdxl.dtype)

    for step in sdxl.steps:
        x = sdxl(
            x,
            step=step,
            clip_text_embedding=clip_text_embedding,
            pooled_text_embedding=pooled_text_embedding,
            time_ids=time_ids,
        )
    predicted_image = sdxl.lda.latents_to_image(x)

    ensure_similar_images(predicted_image, expected_image, min_psnr=40, min_ssim=0.98)
