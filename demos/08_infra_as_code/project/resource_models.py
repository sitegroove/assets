"""Typed Pydantic resource models for fake GCP assets.

Each GCP resource kind is a distinct Asset subclass with domain-specific
fields instead of a generic ``properties`` bag.  This gives type safety,
IDE autocompletion, and self-documenting configs.
"""

from __future__ import annotations

from assets import Asset, AssetField


class GcpResource(Asset):
    """Base class for all GCP resource assets."""

    project_id: str
    location: str = "global"
    labels: dict[str, str] = AssetField(default_factory=dict)

    # Runtime tracking fields (excluded from fingerprint)
    last_observed_status: str = AssetField(default="unknown", fingerprint=False)
    drift_score: int = AssetField(default=0, fingerprint=False)


class Network(GcpResource):
    """VPC network."""

    type: str = "gcp_network"
    auto_create_subnetworks: bool = False
    routing_mode: str = "GLOBAL"


class Subnet(GcpResource):
    """VPC subnetwork."""

    type: str = "gcp_subnetwork"
    ip_cidr_range: str
    private_ip_google_access: bool = True


class ServiceAccount(GcpResource):
    """IAM service account."""

    type: str = "gcp_service_account"
    account_id: str
    display_name: str = ""
    roles: list[str] = AssetField(default_factory=list)


class StorageBucket(GcpResource):
    """Cloud Storage bucket."""

    type: str = "gcp_storage_bucket"
    bucket_name: str
    versioning: bool = False
    uniform_bucket_level_access: bool = True


class PubSubTopic(GcpResource):
    """Pub/Sub topic."""

    type: str = "gcp_pubsub_topic"
    topic_name: str
    message_retention_duration: str = "604800s"


class BigQueryDataset(GcpResource):
    """BigQuery dataset."""

    type: str = "gcp_bigquery_dataset"
    dataset_id: str
    default_table_expiration_ms: int = 0


class DataflowJob(GcpResource):
    """Dataflow pipeline job."""

    type: str = "gcp_dataflow_job"
    template: str
    worker_machine_type: str = "e2-standard-2"
    max_workers: int = 10


class CloudRunService(GcpResource):
    """Cloud Run service."""

    type: str = "gcp_cloud_run_service"
    service_name: str
    image: str
    min_instances: int = 0
    max_instances: int = 10
