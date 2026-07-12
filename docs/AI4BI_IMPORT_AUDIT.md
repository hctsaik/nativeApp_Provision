# AI4BI import audit

AST audit of `vendor/AI4BI/ai4bi` on 2026-07-12 found these third-party roots: anthropic, duckdb, matplotlib, numpy, pandas, plotly, pyarrow, pydantic, scipy, and streamlit. They are now explicitly pinned in `plugins/bi/modules/ai4bi/plugin.yaml` and mirrored by `app.json`; AI4BI no longer silently inherits an undocumented engine environment.

The version pins reflect the validated Python 3.11 development baseline. Updating AI4BI requires rerunning the import audit and offline wheel resolution.
