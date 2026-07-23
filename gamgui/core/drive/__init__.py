"""Bounded Google Drive administration backed by delegated Drive API calls."""

from .client import DriveAPIClient, DriveAPIError, ServiceAccountTokenProvider
from .models import (
    DriveFile,
    DrivePage,
    DrivePermission,
    OperationManifest,
    OperationTarget,
    PreviewStream,
    TransferResult,
)
from .operations import DriveOperationStore, default_operation_path
from .service import (
    ConnectorDirectoryResolver,
    DriveSafetyError,
    DriveService,
    InternalPrincipal,
)

__all__ = [
    "ConnectorDirectoryResolver",
    "DriveAPIClient",
    "DriveAPIError",
    "DriveFile",
    "DriveOperationStore",
    "DrivePage",
    "DrivePermission",
    "DriveSafetyError",
    "DriveService",
    "InternalPrincipal",
    "OperationManifest",
    "OperationTarget",
    "PreviewStream",
    "ServiceAccountTokenProvider",
    "TransferResult",
    "default_operation_path",
]
