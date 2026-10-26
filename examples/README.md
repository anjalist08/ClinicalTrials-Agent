# Example runs (live ClinicalTrials.gov data)

> **Not generated yet.** The build environment for this repository had no network access to
> `clinicaltrials.gov` / `api.openai.com`, and this folder only ever holds real outputs, never mocked ones.
> Generate them (≈1–2 minutes) with:
>
> ```bash
> python scripts/run_examples.py          # writes NN_*.request.json / .response.json / .audit.json + this index
> ```

The seven example requests are defined in `scripts/run_examples.py` (`EXAMPLES`) and listed in the main
README, §9. For each one the script saves the request, the exact `POST /v1/query` response, and an
independent citation audit that re-fetches a random sample of cited trials from
`https://clinicaltrials.gov/api/v2/studies/{nct_id}` and checks every evidence excerpt.
