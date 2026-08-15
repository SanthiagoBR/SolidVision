"""Development tooling for building and materializing SolidVision's demo dataset.

Not part of the shipped application -- see RFC-022. Kept as a separate
top-level package (not `app/infrastructure/`) because none of this runs in
production, and named `dataset_tools` rather than `datasets` to avoid
shadowing HuggingFace's `datasets` package once it is installed alongside
`transformers`.
"""
