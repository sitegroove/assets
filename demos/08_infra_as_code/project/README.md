# Fake Google Cloud Config Project

This project demonstrates Terraform-style Python configuration using the
`assets` library.

- `resource_models.py`: shared Pydantic resource schema.
- `platform_defaults.py`: shared constants/labels.
- `resources/*.py`: one resource declaration per file.

The files are declarative only. They do not create cloud resources.

Run the full workflow:

```bash
python demos/08_fake_google_cloud_resources/main.py
```
