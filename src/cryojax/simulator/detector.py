"""
Abstraction of electron detectors in a cryo-EM image.
"""

__all__ = ["Detector", "NullDetector", "GaussianDetector", "pixelize_image"]

import jax
import jax.numpy as jnp

from abc import ABCMeta, abstractmethod
from typing import Any, Optional
from functools import partial

from .noise import GaussianNoise
from .kernel import Kernel, Constant
from ..utils import scale, irfft
from ..core import dataclass, field, Array, ArrayLike, Parameter, CryojaxObject


@partial(dataclass, kw_only=True)
class Detector(CryojaxObject, metaclass=ABCMeta):
    """
    Base class for an electron detector.

    Attributes
    ----------
    pixel_size : `cryojax.core.Parameter`
        The pixel size measured by the detector.
        This is in dimensions of physical length.
    method : `bool`, optional
        The interpolation method used for measuring
        the image at the ``pixel_size``.
    """

    pixel_size: Optional[Parameter] = field(default=None)
    method: str = field(pytree_node=False, default="bicubic")

    def pixelize(self, image: ArrayLike, resolution: float) -> Array:
        """
        Pixelize an image at a given resolution to
        the detector pixel size.
        """
        pixel_size = resolution if self.pixel_size is None else self.pixel_size
        pixelized = pixelize_image(
            image,
            resolution,
            pixel_size,
            method=self.method,
            antialias=False,
        )
        return pixelized

    @abstractmethod
    def sample(
        self, freqs: ArrayLike, image: Optional[ArrayLike] = None
    ) -> Array:
        """Sample a realization from the detector noise model."""
        raise NotImplementedError


@partial(dataclass, kw_only=True)
class NullDetector(Detector):
    """
    A 'null' detector.
    """

    def sample(
        self, freqs: ArrayLike, image: Optional[ArrayLike] = None
    ) -> Array:
        return jnp.zeros(jnp.asarray(freqs).shape[0:-1])


@partial(dataclass, kw_only=True)
class GaussianDetector(GaussianNoise, Detector):
    """
    A detector with a gaussian noise model. By default,
    this is a white noise model.

    Attributes
    ----------
    variance : `cryojax.simulator.Kernel`
        A kernel that computes the variance
        of the detector noise. By default,
        ``Constant()``.
    """

    variance: Kernel = field(default_factory=Constant)

    def sample(
        self, freqs: ArrayLike, image: Optional[ArrayLike] = None
    ) -> Array:
        return irfft(super().sample(freqs))


@partial(jax.jit, static_argnames=["method", "antialias"])
def pixelize_image(
    image: ArrayLike, resolution: float, pixel_size: float, **kwargs
):
    """
    Measure an image at a given pixel size using interpolation.

    For more detail, see ``cryojax.utils.interpolation.scale``.

    Parameters
    ----------
    image : `Array`, shape `(N1, N2)`
        The image to be magnified.
    resolution : `float`
        The resolution, in physical length, of
        the image.
    pixel_size : `float`
        The pixel size of the detector.
    """
    scale_factor = resolution / pixel_size
    s = jnp.array([scale_factor, scale_factor])
    return scale(image, image.shape, s, **kwargs)
