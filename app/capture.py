from __future__ import annotations

from dataclasses import dataclass

import mss
import numpy as np


@dataclass
class CaptureRegion:
    x: int
    y: int
    width: int
    height: int


def capture_region(region: CaptureRegion) -> np.ndarray:
    with mss.mss() as sct:
        monitor = {
            "left": region.x,
            "top": region.y,
            "width": region.width,
            "height": region.height,
        }
        shot = sct.grab(monitor)
        bgra = np.asarray(shot, dtype=np.uint8)
        return bgra[:, :, :3][:, :, ::-1].copy()
