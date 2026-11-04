"""Plain JSON persistence. One writer process; no database or hidden index engine."""
from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import shutil
import time
from threading import RLock
from typing import TypeVar
from uuid import UUID, uuid4

from pydantic import ValidationError

from .errors import AppError
from .models import Record, Project

R = TypeVar("R", bound=Record)
KINDS = {"assets", "jobs", "intents", "analyses", "events", "tracks", "exports", "corrections", "evidence", "windows", "scenes"}


def file_sha256(path: Path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def atomic_replace(source: Path,destination: Path):
    """Keep atomic replacement through short-lived Windows sharing locks."""
    for attempt in range(5):
        try:
            os.replace(source,destination)
            return
        except PermissionError as exc:
            # Indexers/readers may deny rename briefly. Never truncate the old
            # record or retry disk-full/other errors as if they were sharing.
            if getattr(exc,'winerror',None) not in (5,32,33) or attempt==4:
                raise
            time.sleep(.01*2**attempt)


def canonical_id(value) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise AppError("INVALID_ID", "Invalid resource identifier", 422) from None


def _is_uuid(value):
    try:UUID(value);return True
    except (ValueError,TypeError):return False


class FileStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.projects = self.root / "projects"
        self.projects.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.artifact_lock = RLock()
        self._media_leases = {}
        self._process_lock = None

    def acquire_process_lock(self):
        handle = (self.root / ".writer.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError("DATA_DIR is already used by another process; run one worker") from None
        self._process_lock = handle

    def release_process_lock(self):
        if self._process_lock:
            self._process_lock.close()
            self._process_lock = None

    @contextmanager
    def mutation(self):
        with self.lock:
            yield

    @contextmanager
    def media_lease(self, project_id):
        """Keep project files alive while extraction or a response reads them."""
        key=canonical_id(project_id)
        with self.lock:
            self.load(project_id,"project",Project)
            self._media_leases[key]=self._media_leases.get(key,0)+1
        try:
            yield
        finally:
            with self.lock:
                self._media_leases[key]-=1
                if self._media_leases[key]==0:
                    del self._media_leases[key]

    def read_json(self, path: Path):
        if not path.resolve().is_relative_to(self.root):
            raise AppError("INVALID_PATH","Record path is outside storage",400)
        try:
            with self.lock:
                return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise AppError("NOT_FOUND","Resource not found",404) from None
        except (ValueError,OSError):
            raise AppError("CORRUPT_RECORD","Saved state could not be read; review project files",500) from None

    def project_dir(self, project_id) -> Path:
        path = (self.projects / canonical_id(project_id)).resolve()
        if not path.is_relative_to(self.projects) or path == self.projects:
            raise AppError("INVALID_PATH", "Project path is invalid", 400)
        return path

    def path(self, project_id, relative: str) -> Path:
        root = self.project_dir(project_id)
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or path == root:
            raise AppError("INVALID_PATH", "Media path is outside its project", 400)
        return path

    def _record_path(self, project_id, kind: str, record_id=None):
        if kind == "project":
            return self.project_dir(project_id) / "project.json"
        if kind not in KINDS:
            raise ValueError("Unknown record kind")
        return self.path(project_id, f"{kind}/{canonical_id(record_id)}.json")

    def write_json(self, path: Path, data):
        if not path.resolve().is_relative_to(self.root):
            raise AppError("INVALID_PATH", "Record path is outside storage", 400)
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                atomic_replace(temporary, path)
                if os.name != "nt":
                    fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            finally:
                temporary.unlink(missing_ok=True)

    def save(self, project_id, kind: str, record: Record):
        if kind == "project" and record.id != UUID(canonical_id(project_id)):
            raise AppError("OWNERSHIP", "Project ID does not match record", 400)
        if kind != "project" and getattr(record, "project_id", None) != UUID(canonical_id(project_id)):
            raise AppError("OWNERSHIP", "Record does not belong to project", 400)
        with self.lock:
            data=record.model_dump(mode='json')
            if kind=='tracks':
                from .track_storage import pack_track
                data=pack_track(self,project_id,record)
            self.write_json(self._record_path(project_id, kind, record.id), data)

    def write_bytes(self,path: Path,data: bytes):
        if not path.resolve().is_relative_to(self.root):
            raise AppError('INVALID_PATH','Artifact path is outside storage',400)
        with self.lock:
            path.parent.mkdir(parents=True,exist_ok=True)
            temporary=path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
            try:
                with temporary.open('xb') as stream:
                    stream.write(data);stream.flush();os.fsync(stream.fileno())
                atomic_replace(temporary,path)
                if os.name!='nt':
                    fd=os.open(path.parent,os.O_RDONLY)
                    try:os.fsync(fd)
                    finally:os.close(fd)
            finally:temporary.unlink(missing_ok=True)

    def load(self, project_id, kind: str, model: type[R], record_id=None) -> R:
        path = self._record_path(project_id, kind, record_id)
        try:
            # Windows readers otherwise briefly prevent atomic replacement of
            # the same file while the UI polls a worker's stage updates.
            with self.lock:
                data = json.loads(path.read_text(encoding="utf-8"))
                if kind=='tracks':
                    from .track_storage import unpack_track
                    data=unpack_track(self,canonical_id(project_id),canonical_id(record_id),data)
        except FileNotFoundError:
            raise AppError("NOT_FOUND", "Resource not found", 404) from None
        except (ValueError, OSError):
            raise AppError("CORRUPT_RECORD", "Saved state could not be read; review project files", 500) from None
        try:
            record = model.model_validate(data)
        except ValidationError:
            raise AppError("CORRUPT_RECORD", "Saved state failed schema validation", 500) from None
        expected_id = UUID(canonical_id(project_id if kind == "project" else record_id))
        if record.id != expected_id or (kind != "project" and getattr(record, "project_id", None) != UUID(canonical_id(project_id))):
            raise AppError("CORRUPT_RECORD", "Saved record ownership is invalid", 500)
        return record

    def list_records(self, project_id, kind: str, model: type[R]) -> list[R]:
        records,errors=self.scan_records(project_id,kind,model)
        if errors:
            error=errors[0]
            raise AppError(error['code'],error['message'],500,details={'record_id':error['record_id'],'record_kind':kind})
        return records

    def scan_records(self,project_id,kind: str,model: type[R]):
        self.load(project_id, "project", Project)
        if kind not in KINDS:
            raise ValueError("Unknown record kind")
        records,errors = [],[]
        for path in sorted(self.path(project_id, kind).glob("*.json")):
            # Adjacent artifact manifests use .manifest.json, not UUID record names.
            try:
                UUID(path.stem)
            except ValueError:
                continue
            try:records.append(self.load(project_id, kind, model, path.stem))
            except AppError as exc:errors.append({**exc.envelope(),'record_id':path.stem,'record_kind':kind})
        return records,errors

    def scan_projects(self):
        records,errors=[],[]
        for path in sorted(self.projects.glob('*/project.json')):
            try:records.append(self.load(path.parent.name,'project',Project))
            except AppError as exc:
                errors.append({**exc.envelope(),'record_id':path.parent.name if _is_uuid(path.parent.name) else None,'record_kind':'project'})
        return sorted(records,key=lambda p:p.created_at,reverse=True),errors

    def list_projects(self) -> list[Project]:
        result = []
        for path in sorted(self.projects.glob("*/project.json")):
            result.append(self.load(path.parent.name, "project", Project))
        return sorted(result, key=lambda p: p.created_at, reverse=True)

    def create_project(self, project: Project):
        with self.lock:
            root = self.project_dir(project.id)
            root.mkdir(exist_ok=False)
            self.save(project.id, "project", project)

    def delete_project(self, project_id):
        with self.lock:
            self.load(project_id, "project", Project)
            if self._media_leases.get(canonical_id(project_id),0):
                raise AppError("PROJECT_BUSY","Media is being read; retry deletion after playback or inspection finishes",409,True)
            root = self.project_dir(project_id)
            # root is a canonical server UUID confined to projects, checked again above.
            shutil.rmtree(root)
