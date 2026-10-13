"""Business Profile loading, validation (schema + invariants L1-L11), fact refs and storage."""

from shopfront_server.profile.directory import DirectoryEntry, build_directory_entry
from shopfront_server.profile.factref import FactRef, FactRefError, ResolvedFact, entity_ref, resolve
from shopfront_server.profile.invariants import LoadReport, Violation
from shopfront_server.profile.loader import LoadedProfile, load_profile, load_profile_file
from shopfront_server.profile.model import JsonObj, Profile, thaw
from shopfront_server.profile.snapshots import DirectorySnapshotProvider, NullSnapshotProvider, SnapshotProvider
from shopfront_server.profile.statable import Statability, disputed_by, statability
from shopfront_server.profile.store import CachingProfileStore, InMemoryProfileStore, ProfileStore, load_profile_dir

__all__ = [
    "CachingProfileStore",
    "DirectoryEntry",
    "DirectorySnapshotProvider",
    "FactRef",
    "FactRefError",
    "InMemoryProfileStore",
    "JsonObj",
    "LoadReport",
    "LoadedProfile",
    "NullSnapshotProvider",
    "Profile",
    "ProfileStore",
    "ResolvedFact",
    "SnapshotProvider",
    "Statability",
    "Violation",
    "build_directory_entry",
    "disputed_by",
    "entity_ref",
    "load_profile",
    "load_profile_dir",
    "load_profile_file",
    "resolve",
    "statability",
    "thaw",
]
