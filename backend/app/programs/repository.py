"""Immutable compiler/program history and exact source/version review binding."""
import hashlib
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, ValidationError

from ..errors import AppError
from ..models import Asset, Model, now
from ..repository import ArtifactReference, Envelope, Repository, encoded, fingerprint, reference
from ..source_evidence import SourceEvidenceAdapter
from ..resources import MediaBudget
from .ast import InterpretationIssue, VisualProgram
from .compiler import Compilation
from .outcomes import CompileOutcome, classify_compilation
from .validation import require_valid_program


class HumanEdit(Model):
    kind: Literal['confirm_assumptions','ast_patch','muse_revision'] = 'confirm_assumptions'
    base_version: int = Field(ge=1)
    base_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    confirmed_assumptions: list[InterpretationIssue] = Field(default_factory=list,max_length=32)
    revision: dict | None = None
    created_at: datetime = Field(default_factory=now)


class StoredProgram(Model):
    program: VisualProgram
    outcome: CompileOutcome
    compilation_id: UUID
    human_edit: HumanEdit | None = None


def reviewable(data):
    ast=data.program.ast
    return (data.outcome.approval_allowed and data.outcome.state=='ready_for_review'
            and not any(i.material for i in [*ast.ambiguities,*ast.unrepresented_clauses,*ast.assumptions])
            and all(c.status=='represented' for c in ast.clauses))


class ReviewRequest(Model):
    version: int = Field(ge=1, strict=True)
    program_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    source_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class ConfirmAssumptionsRequest(ReviewRequest):
    accepted_codes: list[Annotated[str,Field(pattern=r'^[A-Z][A-Z0-9_]{0,63}$')]] = Field(min_length=1,max_length=32)


def confirm_assumptions(store,settings,project_id,program_id,body):
    body=ConfirmAssumptionsRequest.model_validate(body)
    with store.mutation():
        base,data=load_program(store,project_id,program_id,body.version)
        if fingerprint(base)!=body.program_fingerprint or base.source_sha256!=body.source_sha256:
            raise AppError('STALE_REVIEW','Inspect this source/version again before confirming assumptions',409)
        if len(set(body.accepted_codes))!=len(body.accepted_codes):
            raise AppError('INVALID_ASSUMPTION_SELECTION','Assumption codes must be unique',422)
        repo=Repository(store)
        paths=store.path(project_id,f'programs/{program_id}').glob('v*.json')
        try:latest=max(int(p.stem[1:]) for p in paths)
        except ValueError:raise AppError('CORRUPT_PROGRAM','Program version filename is invalid',500) from None
        if latest!=body.version:
            previous,previous_data=load_program(store,project_id,program_id,latest)
            edit=previous_data.human_edit
            if (edit and edit.base_fingerprint==body.program_fingerprint
                    and {a.code for a in edit.confirmed_assumptions}==set(body.accepted_codes)):
                return previous,previous_data
            raise AppError('STALE_VERSION','A newer version exists; inspect it before another edit',409)
        selected=[a for a in data.program.ast.assumptions if a.code in body.accepted_codes]
        if (len(selected)!=len(body.accepted_codes) or any(not a.material for a in selected)
                or len({a.code for a in selected})!=len(selected)):
            raise AppError('INVALID_ASSUMPTION_SELECTION','Select only existing unresolved material assumptions',422)
        adapter=SourceEvidenceAdapter(store,settings,project_id,base.source_asset_id)
        raw=data.program.ast.model_dump(mode='json',by_alias=True)
        for note in raw['assumptions']:
            if note['code'] in body.accepted_codes:note['material']=False
        ast=require_valid_program(raw,source_duration_ms=adapter.asset.metadata.duration_ms,require_clauses=True)
        compilation_record=repo.load(project_id,'compilations',data.compilation_id)
        compilation=Compilation.model_validate(compilation_record.payload['compilation'])
        compilation=compilation.model_copy(update={'draft':compilation.draft.model_copy(update={'ast':ast})})
        outcome=classify_compilation(compilation)
        program=data.program.model_copy(update={'version':base.version+1,'ast':ast,
            'compiler':data.program.compiler.model_copy(update={'origin':'user_edit','compiler_version':'human-assumptions-1','compiled_at':now()})})
        updated=StoredProgram(program=program,outcome=outcome,compilation_id=data.compilation_id,
            human_edit=HumanEdit(base_version=base.version,base_fingerprint=fingerprint(base),confirmed_assumptions=selected))
        envelope=repo.publish(Envelope(kind='programs',id=base.id,version=program.version,
            project_id=base.project_id,source_asset_id=base.source_asset_id,source_sha256=base.source_sha256,
            references=base.references,payload=updated.model_dump(mode='json',by_alias=True)))
        return envelope,updated


def load_program(store, project_id, program_id, version=1):
    envelope=Repository(store).load(project_id,'programs',program_id,version)
    try:
        data=StoredProgram.model_validate(envelope.payload)
    except ValidationError:
        raise AppError('CORRUPT_PROGRAM','Program payload failed validation',500) from None
    program=data.program
    if (program.program_id != envelope.id or program.version != envelope.version
            or program.project_id != envelope.project_id or program.source_asset_id != envelope.source_asset_id
            or program.source_sha256 != envelope.source_sha256):
        raise AppError('OWNERSHIP','Program payload differs from its owning envelope',409)
    return envelope,data


def review_id(program):
    return uuid5(program.id,f'program-review:{program.version}:{fingerprint(program)}')


def assert_reviewed(store, program):
    checked,data=load_program(store,program.project_id,program.id,program.version)
    if checked != program or not reviewable(data):
        raise AppError('PROGRAM_NOT_REVIEWABLE','Program has unresolved or unsupported interpretation issues',409)
    try:
        review=Repository(store).load(program.project_id,'program_reviews',review_id(program))
    except AppError as exc:
        if exc.code=='NOT_FOUND':
            raise AppError('PROGRAM_REVIEW_REQUIRED','Approve this exact program/source version before running',409) from None
        raise
    if (review.program != reference(program) or review.payload.get('decision') != 'approved'
            or review.payload.get('program_fingerprint') != fingerprint(program)):
        raise AppError('STALE_REVIEW','Review does not approve this exact program fingerprint',409)
    return review


def approve_program(store, settings, project_id, program_id, body):
    body=ReviewRequest.model_validate(body)
    with store.mutation():
        program,data=load_program(store,project_id,program_id,body.version)
        if body.program_fingerprint != fingerprint(program) or body.source_sha256 != program.source_sha256:
            raise AppError('STALE_REVIEW','Source/version fingerprint changed; inspect the requested version again',409)
        if not reviewable(data):
            raise AppError('PROGRAM_NOT_REVIEWABLE','Resolve material interpretation issues before approval',409)
        adapter=SourceEvidenceAdapter(store,settings,project_id,program.source_asset_id)
        require_valid_program(data.program.ast,source_duration_ms=adapter.asset.metadata.duration_ms,require_clauses=True)
        identifier=review_id(program)
        repo=Repository(store)
        if repo.path(project_id,'program_reviews',identifier).exists():
            return assert_reviewed(store,program)
        return repo.publish(Envelope(kind='program_reviews',id=identifier,project_id=program.project_id,
            source_asset_id=program.source_asset_id,source_sha256=program.source_sha256,program=reference(program),
            payload={'decision':'approved','program_fingerprint':fingerprint(program),
                     'source_sha256':program.source_sha256,'review_scope':'exact_program_version_and_source'}))


def save_compilation(store, asset, compilation_id, compilation, provider_artifact=None):
    repo=Repository(store)
    outcome=classify_compilation(compilation)
    artifacts=[provider_artifact] if provider_artifact else []
    if compilation.revalidation_of:
        previous=repo.load(asset.project_id,'compilations',compilation.revalidation_of)
        original=Compilation.model_validate(previous.payload['compilation'])
        if (previous.source_sha256!=asset.sha256 or original.original_instruction!=compilation.original_instruction
                or original.source!=compilation.source or original.compiler!=compilation.compiler
                or encoded(original.provider_output)!=encoded(compilation.provider_output)
                or original.draft!=compilation.draft):
            raise AppError('REVALIDATION_MISMATCH','Revalidation must preserve the exact original source and provider proposal',409)
        # Compilation dependencies are a strictly ordered repository layer.
        # Pin the immutable parent bytes as an artifact rather than creating a
        # same-layer reference or relaxing that global dependency invariant.
        parent_path=repo.path(asset.project_id,'compilations',previous.id,previous.version)
        parent_bytes=parent_path.read_bytes()
        artifacts.append(ArtifactReference(path=parent_path.relative_to(store.project_dir(asset.project_id)).as_posix(),
            sha256=hashlib.sha256(parent_bytes).hexdigest(),byte_size=len(parent_bytes),media_type='application/json'))
    record=repo.publish(Envelope(kind='compilations',id=compilation_id,project_id=asset.project_id,
        source_asset_id=asset.id,source_sha256=asset.sha256,artifacts=artifacts,
        payload={'compilation':compilation.model_dump(mode='json',by_alias=True),
                 'outcome':outcome.model_dump(mode='json')}))
    if compilation.draft.ast is None:
        return record,None
    program=VisualProgram(project_id=asset.project_id,source_asset_id=asset.id,source_sha256=asset.sha256,
        ast=compilation.draft.ast,compiler=compilation.compiler,explanation=compilation.draft.explanation)
    data=StoredProgram(program=program,outcome=outcome,compilation_id=str(record.id))
    envelope=repo.publish(Envelope(kind='programs',id=program.program_id,version=program.version,
        project_id=asset.project_id,source_asset_id=asset.id,source_sha256=asset.sha256,
        references=[reference(record)],payload=data.model_dump(mode='json',by_alias=True)))
    return record,envelope


def provider_recording(store, asset, compilation_id, output, metadata, *, settings=None):
    raw=encoded({'output':output,'provenance':metadata})
    if settings is not None:
        MediaBudget(store.project_dir(asset.project_id),settings).check(len(raw))
    relative=f'programs/compilation-artifacts/{compilation_id}.provider.json'
    store.write_bytes(store.path(asset.project_id,relative),raw)
    return ArtifactReference(path=relative,sha256=hashlib.sha256(raw).hexdigest(),byte_size=len(raw),media_type='application/json')
