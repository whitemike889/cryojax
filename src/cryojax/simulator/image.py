"""
Routines to model image formation.
"""

from __future__ import annotations

__all__ = [
    "Image",
    "ScatteringImage",
    "OpticsImage",
    "DetectorImage",
    "GaussianImage",
]

from abc import ABCMeta, abstractmethod
from typing import Union, Optional
from dataclasses import InitVar

import jax.numpy as jnp

from .filters import LowpassFilter
from .noise import GaussianNoise
from .state import PipelineState
from .ice import NullIce
from .exposure import rescale_image
from ..utils import fft, ifft, irfft
from ..core import dataclass, field, Array, Scalar
from . import Filter, Mask, Specimen, ScatteringConfig


@dataclass
class Image(metaclass=ABCMeta):
    """
    Base class for an imaging model. Note that the
    model is a PyTree and is therefore immmutable.

    Use ``ImageModel.update`` to return a new model
    with modified parameters, and call ``ImageModel``
    to evaluate the model.

    Attributes
    ----------
    scattering : `cryojax.simulator.ScatteringConfig`
        The image and scattering model configuration.
    specimen : `cryojax.simulator.Specimen`
        The specimen from which to render images.
    state : `cryojax.simulator.PipelineState`
        The parameter state of the model.
    filters : `list[Filter]`
        A list of filters to apply to the image. By default, this is a
        ``LowpassFilter`` with used for antialiasing.
    masks : `list[Mask]`
        A list of masks to apply to the image. By default, there are no
        masks.
    observed : `jax.Array`, optional
        The observed data in Fourier space. This must be the same
        shape as ``scattering.shape``. ``ImageModel.observed`` will return
        the data with the filters applied.
    """

    state: PipelineState
    specimen: Specimen
    scattering: ScatteringConfig = field(pytree_node=False)

    filters: list[Filter] = field(pytree_node=False, init=False)
    masks: list[Mask] = field(pytree_node=False, init=False)
    observed: Optional[Array] = field(pytree_node=False, init=False)

    filters: InitVar[list[Filter] | None] = None
    masks: InitVar[list[Mask] | None] = None
    observed: InitVar[Array | None] = None
    set_observed: bool = field(pytree_node=False, default=True)

    def __post_init__(self, filters, masks, observed):
        scattering = self.scattering
        # Set filters
        if filters is None:
            antialias = LowpassFilter(scattering.padded_freqs, cutoff=0.667)
            filters = [antialias]
        object.__setattr__(self, "filters", filters)
        # Set masks
        if masks is None:
            masks = []
        object.__setattr__(self, "masks", masks)
        # Set observed data
        if observed is not None and self.set_observed:
            assert scattering.shape == observed.shape
            observed = irfft(observed)
            mean, std = observed.mean(), observed.std()
            observed = fft(scattering.pad(observed, constant_values=mean))
            assert scattering.padded_shape == observed.shape
            observed = scattering.crop(irfft(self.filter(observed)))
            assert scattering.shape == observed.shape
            observed = rescale_image(observed, std, mean)
            observed = fft(self.mask(observed))
        object.__setattr__(self, "observed", observed)

    @abstractmethod
    def render(
        self,
        state: Optional[PipelineState] = None,
        specimen: Optional[Specimen] = None,
        view: bool = True,
    ) -> Array:
        """Render an image given a parameter set."""
        raise NotImplementedError

    @abstractmethod
    def sample(
        self,
        state: Optional[PipelineState] = None,
        specimen: Optional[Specimen] = None,
        view: bool = True,
    ) -> Array:
        """Sample the an image from a realization of the noise"""
        raise NotImplementedError

    @abstractmethod
    def log_likelihood(
        self,
        state: Optional[PipelineState] = None,
        specimen: Optional[Specimen] = None,
    ) -> Scalar:
        """Evaluate the log-likelihood of the data given a parameter set."""
        raise NotImplementedError

    def __call__(
        self,
        params: dict = {},
    ) -> Union[Array, Scalar]:
        """
        Evaluate the model at a parameter set.

        If ``ImageModel.observed = None``, sample an image from
        a noise model. Otherwise, compute the log likelihood.
        """
        state = self.state.update(**params)
        specimen = self.specimen.update(**params)
        if self.observed is None:
            return self.sample(state=state, specimen=specimen)
        else:
            return self.log_likelihood(state=state, specimen=specimen)

    def filter(self, image: Array) -> Array:
        """Apply filters to image."""
        for filter in self.filters:
            image = filter(image)
        return image

    def mask(self, image: Array) -> Array:
        """Apply masks to image."""
        for mask in self.masks:
            image = mask(image)
        return image

    def residuals(
        self,
        state: Optional[PipelineState] = None,
        specimen: Optional[Specimen] = None,
    ):
        """Return the residuals between the model and observed data."""
        state = state or self.state
        specimen = specimen or self.specimen
        simulated = self.render(state=state, specimen=specimen)
        residuals = self.observed - simulated
        return residuals

    def update(self, **params: dict) -> Image:
        """Return a new ImageModel based on a new PipelineState."""
        state = self.state.update(**params)
        specimen = self.specimen.update(**params)
        return self.replace(
            state=state,
            specimen=specimen,
            set_observed=False,
        )


@dataclass
class ScatteringImage(Image):
    """
    Compute the scattering pattern on the imaging plane.
    """

    def render(
        self,
        state: Optional[PipelineState] = None,
        specimen: Optional[Specimen] = None,
        view: bool = True,
    ) -> Array:
        """Render the scattering pattern of the specimen."""
        state = state or self.state
        specimen = specimen or self.specimen
        scattering = self.scattering
        # Compute scattering at image plane.
        specimen = self.specimen.view(state.pose)
        scattering_image = specimen.scatter(scattering)
        # Apply filters
        scattering_image = self.filter(scattering_image)
        # Optionally crop and mask image
        if view:
            scattering_image = fft(
                self.mask(
                    state.exposure.rescale(
                        scattering.crop(ifft(scattering_image))
                    )
                )
            )

        return scattering_image

    def sample(
        self,
        state: Optional[PipelineState] = None,
        specimen: Optional[Specimen] = None,
    ) -> Array:
        """Sample the scattering pattern of the specimen and ice."""
        state = state or self.state
        specimen = specimen or self.specimen
        scattering = self.scattering
        padded_freqs, pixel_size = (
            scattering.padded_freqs,
            scattering.pixel_size,
        )
        # Render an image with no ice
        scattering_image = self.render(state=state, specimen=specimen)
        # Sample from ice distribution
        ice = self.filter(state.ice.sample(padded_freqs / pixel_size))
        # View ice
        ice = fft(self.mask(scattering.crop(ifft(ice))))
        # Create an icy image in the exit plane
        icy_image = scattering_image + ice

        return icy_image

    def log_likelihood(
        self,
        state: Optional[PipelineState] = None,
        specimen: Optional[Specimen] = None,
    ) -> Scalar:
        raise NotImplementedError


@dataclass
class OpticsImage(ScatteringImage):
    """
    Compute the image at the detector plane,
    moduated by a CTF at a given electron dose.
    """

    def render(
        self,
        state: Optional[PipelineState] = None,
        specimen: Optional[Specimen] = None,
    ) -> Array:
        """Render the image in the detector plane."""
        state = state or self.state
        specimen = specimen or self.specimen
        scattering = self.scattering
        padded_freqs, pixel_size = (
            scattering.padded_freqs,
            scattering.pixel_size,
        )
        # Compute scattering at object plane.
        scattering_image = super().render(
            state=state, specimen=specimen, view=False
        )
        # Compute and apply CTF
        ctf = state.optics(padded_freqs / pixel_size)
        optics_image = state.optics.apply(ctf, scattering_image)
        # Crop, rescale, and mask image
        optics_image = fft(
            self.mask(
                state.exposure.rescale(scattering.crop(ifft(optics_image)))
            )
        )

        return optics_image

    def sample(
        self,
        state: Optional[PipelineState] = None,
        specimen: Optional[Specimen] = None,
    ) -> Array:
        """
        Sample the image in the detector plane.
        """
        state = state or self.state
        specimen = specimen or self.specimen
        scattering = self.scattering
        padded_freqs, pixel_size = (
            scattering.padded_freqs,
            scattering.pixel_size,
        )
        # Compute image
        optics_image = self.render(state=state, specimen=specimen)
        # Sample from ice distribution and apply ctf to it
        ctf = state.optics(padded_freqs / pixel_size)
        ice = self.filter(state.ice.sample(padded_freqs / pixel_size))
        ice = state.optics.apply(ctf, ice)
        # Crop and mask ice
        ice = fft(self.mask(scattering.crop(ifft(ice))))
        # Create icy image at detector plane
        icy_image = optics_image + ice

        return icy_image


@dataclass
class DetectorImage(OpticsImage):
    """
    Compute the readout from the detector from the image
    in the detector plane.
    """

    def sample(
        self,
        state: Optional[PipelineState] = None,
        specimen: Optional[Specimen] = None,
    ) -> Array:
        """Sample an image from the detector readout."""
        state = state or self.state
        specimen = specimen or self.specimen
        scattering = self.scattering
        freqs, pixel_size = scattering.freqs, scattering.pixel_size
        effective_pixel_size = pixel_size * state.optics.magnification
        # Sample image at detector plane
        icy_image = super().sample(state)
        # Sample from noise distribution of detector
        noise = state.detector.sample(freqs / effective_pixel_size)
        # Mask noise
        noise = fft(self.mask(ifft(noise)))
        # Detector readout
        detector_image = icy_image + noise

        return detector_image


@dataclass
class GaussianImage(DetectorImage):
    """
    Sample an image from a gaussian noise model, or compute
    the log-likelihood.

    Note that this computes the likelihood in Fourier space,
    which allows one to model an arbitrary noise power spectrum.
    """

    def __post_init__(self, *args):
        super().__post_init__(*args)
        assert isinstance(self.state.ice, GaussianNoise)
        assert isinstance(self.state.detector, GaussianNoise)

    def log_likelihood(
        self,
        state: Optional[PipelineState] = None,
        specimen: Optional[Specimen] = None,
    ) -> Scalar:
        """Evaluate the log-likelihood of the data given a parameter set."""
        state = state or self.state
        specimen = specimen or self.specimen
        scattering = self.scattering
        freqs, pixel_size = scattering.freqs, scattering.pixel_size
        effective_pixel_size = pixel_size * state.optics.magnification
        # Get residuals
        residuals = self.residuals(state=state, specimen=specimen)
        # Variance from detector
        variance = state.detector.variance(freqs / effective_pixel_size)
        # Variance from ice
        if not isinstance(state.ice, NullIce):
            variance += state.optics(
                freqs / pixel_size
            ) ** 2 * state.ice.variance(freqs / pixel_size)
        loss = jnp.sum((residuals * jnp.conjugate(residuals)) / (2 * variance))
        return loss.real / residuals.size
