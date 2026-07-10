## What does this change and why?

<!-- Link an issue if there is one. -->

## Checklist

- [ ] I added/updated tests in `tests/test_tt.py` for this change
- [ ] `python -m unittest discover -s tests` passes locally
- [ ] Exit codes, tee-on-failure, and the "never larger than raw" guarantee
      still hold (see CONTRIBUTING.md's safety invariants)
- [ ] If this touched agent instructions, `templates/copilot-instructions.md`
      was updated to match `COPILOT_INSTRUCTIONS` in `tt.py`

## How was this tested?

<!-- Unit tests are required, but if you ran this against real
     docker/kubectl/az/aws/gcloud/etc., say so — that's exactly the kind of
     verification this project needs more of. -->
