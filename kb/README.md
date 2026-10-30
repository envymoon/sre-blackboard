# Knowledge base

Runbooks (`runbooks/`) and postmortems (`postmortems/`) for the lab's shop (the OpenTelemetry
Demo), written for this project and public. Each file is Markdown with a front matter block
(`id`, `kind`, `title`, `services`, `version`); `python -m app.kb ingest` chunks, embeds and
stores them per tenant (ADR-0010). This README is not ingested.

The postmortems describe past incidents of the same kinds as the evaluation's core
scenarios, with their own dates and details. The error messages are the real ones the
lab's services print.
