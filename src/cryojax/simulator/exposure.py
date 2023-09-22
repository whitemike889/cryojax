"""
Routines to handle variations in image intensity
due to electron exposure.
"""

from __future__ import annotations

__all__ = ["Exposure", "NullExposure", "UniformExposure", "rescale_image"]

from abc import ABCMeta, abstractmethod
from functools import partial

import jax
import jax.numpy as jnp

from ..core import dataclass, field, Array, ArrayLike, Parameter, CryojaxObject


@dataclass
class Exposure(CryojaxObject, metaclass=ABCMeta):
    """
    An PyTree that controls parameters related to
    variation in the image intensity. For example,
    this includes the incoming electron dose and
    radiation damage.
    """

    @abstractmethod
    def scale(self, image: ArrayLike, real: bool = False) -> Array:
        """
        Return the scaled image.
        """
        raise NotImplementedError


@dataclass
class NullExposure(Exposure):
    """
    A `null` exposure model. Do not change the
    image when it is passsed through the pipeline.
    """

    def scale(self, image: ArrayLike, real: bool = False) -> Array:
        """Return the image unchanged"""
        return image


@dataclass
class UniformExposure(Exposure):
    """
    Scale the signal intensity uniformly.

    Attributes
    ----------
    N : `cryojax.core.Parameter`
        Intensity scaling.
    mu : `cryojax.core.Parameter`
        Intensity offset.
    """

    N: Parameter = field(default=1e5)
    mu: Parameter = field(default=0.0)

    def scale(self, image: ArrayLike, real: bool = False) -> Array:
        """
        Return the scaled image.
        """
        return rescale_image(image, self.N, self.mu, real=real)


@partial(jax.jit, static_argnames=["real"])
def rescale_image(
    image: ArrayLike, N: float, mu: float, *, real: bool = False
) -> Array:
    """
    Normalize so that the image is mean mu
    and standard deviation N in real space.

    Parameters
    ----------
    image : `jax.Array`, shape `(N1, N2)`
        The image in either real or Fourier space.
        If in Fourier space, the zero frequency
        component should be in the center of the image.
    N : `float`
        Intensity scale factor.
    mu : `float`
        Intensity offset.
    real : `bool`
        If ``True``, the given ``image`` is in real
        space. If ``False``, it is in Fourier space.

    Returns
    -------
    rescaled_image : `jax.Array`, shape `(N1, N2)`
        Image rescaled by an offset ``mu`` and scale factor ``N``.
    """
    image = jnp.asarray(image)
    N1, N2 = image.shape
    if real:
        rescaled_image = N * image + mu
    else:
        rescaled_image = N * image
        rescaled_image = rescaled_image.at[0, 0].set(
            rescaled_image[0, 0] + (mu * N1 * N2)
        )
    return rescaled_image
