"""Pydantic request / response models (OpenAPI schema shown on /docs)."""

from datetime import date as date_type
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .config import MAX_SIDE

Band = Literal["red", "nir"]


class MaskSubmission(BaseModel):
    """A 0/1 validity mask for one band of one scene.

    A cell value of 1 means the band sample is valid at that grid cell,
    0 means missing. The mask is a row-major two-dimensional array whose
    dimensions equal the scene grid size.
    """

    date: date_type = Field(
        ..., description="UTC acquisition date of the scene (YYYY-MM-DD)."
    )
    scene_id: str = Field(..., min_length=1, description="Scene identifier.")
    origin_x: int = Field(
        ..., description="Grid x coordinate of the scene's upper-left cell."
    )
    origin_y: int = Field(
        ..., description="Grid y coordinate of the scene's upper-left cell."
    )
    band: Band = Field(..., description="Band: 'red' (红光) or 'nir' (近红外).")
    mask: list[list[Literal[0, 1]]] = Field(
        ...,
        description=(
            "Row-major 0/1 validity mask. Width and height must not exceed "
            f"{MAX_SIDE} cells; rows must share one length."
        ),
    )

    @field_validator("mask")
    @classmethod
    def _validate_mask(cls, mask: list[list[int]]) -> list[list[int]]:
        if not mask or not mask[0]:
            raise ValueError("mask must contain at least one row and one column")
        width = len(mask[0])
        if len(mask) > MAX_SIDE or width > MAX_SIDE:
            raise ValueError(
                f"mask width and height must each be <= {MAX_SIDE} cells"
            )
        for row in mask:
            if len(row) != width:
                raise ValueError("all mask rows must have the same length")
        return mask

    @property
    def height(self) -> int:
        return len(self.mask)

    @property
    def width(self) -> int:
        return len(self.mask[0])


class MaskSubmitResponse(BaseModel):
    date: date_type
    scene_id: str
    band: Band
    origin_x: int
    origin_y: int
    width: int
    height: int
    valid_cells: int = Field(..., description="Number of 1-cells in the mask.")
    coverage_cells_updated: int = Field(
        ..., description="Coverage rows rewritten within this scene's extent."
    )


# Gap categories returned to data operators.
GAP_RED_ONLY = "red_only"            # 仅红光有效（缺近红外）
GAP_NIR_ONLY = "nir_only"            # 仅近红外有效（缺红光）
GAP_DIFFERENT_SCENES = "different_scenes"  # 两波段分属不同景，无共同景
GAP_BOTH_MISSING = "both_missing"    # 红光与近红外都缺

GAP_CATEGORY_LABELS: dict[str, str] = {
    GAP_RED_ONLY: "仅红光有效，缺近红外",
    GAP_NIR_ONLY: "仅近红外有效，缺红光",
    GAP_DIFFERENT_SCENES: "两波段分属不同景，无同一景的红光与近红外",
    GAP_BOTH_MISSING: "红光与近红外均缺报",
}


class GapRun(BaseModel):
    """A run of consecutive gap cells of the same category on one grid row."""

    y: int = Field(..., description="Grid row coordinate.")
    x_start: int = Field(..., description="First cell x coordinate (inclusive).")
    x_end: int = Field(..., description="Last cell x coordinate (inclusive).")
    category: Literal[
        "red_only", "nir_only", "different_scenes", "both_missing"
    ]
    category_label: str = Field(
        ..., description="Chinese explanation shown to data operators."
    )
    length: int = Field(..., description="Number of consecutive cells.")


class GapQueryResponse(BaseModel):
    date: date_type
    origin_x: int
    origin_y: int
    width: int
    height: int
    total_cells: int
    complete_cells: int = Field(
        ...,
        description=(
            "Cells where at least one scene supplies both red and NIR "
            "(植被指数可算)."
        ),
    )
    gap_cells: int
    category_counts: dict[str, int]
    gaps: list[GapRun] = Field(
        ...,
        description="Continuous same-category gap runs, grouped per row.",
    )


class SceneBandInfo(BaseModel):
    band: Band
    valid_cells: int
    updated_at: str


class SceneInfo(BaseModel):
    date: date_type
    scene_id: str
    origin_x: int
    origin_y: int
    width: int
    height: int
    bands: list[SceneBandInfo]
