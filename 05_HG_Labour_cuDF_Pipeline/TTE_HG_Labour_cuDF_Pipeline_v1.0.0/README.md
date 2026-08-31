# TTE HG Labour cuDF Pipeline v1.0.0

This is the production pipeline requested for repeated processing of millions of LifeHRMS labour rows and PROMPT/HG2 weighment rows on the RTX GPU.

## What it produces

- `canonical_labour_worker_day.parquet` — pseudonymized worker-day labour facts.
- `canonical_weighment_event.parquet` — validated weighment events.
- `labour_division_day.parquet/.csv` — daily labour aggregates.
- `weighment_division_day.parquet/.csv` — daily crop aggregates.
- `management_labour_crop_kpis.parquet/.csv` — joined attendance, productivity and labour-cost KPIs.
- Data-quality, schema-rejection, provenance, timing and GPU/CPU benchmark reports.

The first join is deliberately at `date + estate + division`. PROMPT must not be joined to individual workers unless it contains a verified stable worker ID.

## Source priority

Create these folders and place official exports in them:

```text
H:\PROJECT B\00_Official_Exports\LifeHRMS
H:\PROJECT B\00_Official_Exports\PROMPT
```

When official exports are absent, the pipeline uses the known fallback evidence:

- `Labour Attendance TTEL June 2026- V2.xlsx`
- Latest `unique_weighment_audit.csv`

It excludes workforce-planner candidate matrices, assignments, neural outputs and prior pipeline outputs from source discovery.

## Run order

1. Run `CHECK_GPU_STACK.ps1`.
2. Install RAPIDS in WSL2 using the official RAPIDS installation selector if cuDF is missing.
3. Run `RUN_SELF_TEST.ps1`.
4. Run `RUN_HG_LABOUR_CUDF_GPU.ps1`.
5. Enter the same private pseudonymization key on every production run. It is not written to logs or outputs.
6. Run `CHECK_PIPELINE_RESULT.ps1` and review the completion, equivalence, source-alignment and GPU-justification gates.

## Security and evidence boundaries

- Source files are read-only.
- Raw worker IDs are HMAC-pseudonymized before output.
- Names, NICs, contact details, bank fields, addresses and medical fields are excluded.
- Exact employee-level data should remain access-controlled.
- Generated planner data is never accepted as source truth.
- Pipeline completion does not authorize live workforce assignment or payroll use.

## Approval gate

The pipeline is ready for a shadow KPI review only after:

- Both official portal exports are present.
- At least 80% of division-day rows align between labour and weighment sources.
- GPU and CPU aggregate results are equivalent.
- HR and PROMPT source owners reconcile totals.
- Output-folder access controls are confirmed.
