"""
Routes for interacting with block objects.
"""
from typing import List, Optional
from uuid import UUID

import sqlalchemy as sa
from fastapi import Body, Depends, HTTPException, Path, Query, Response, status

from prefect.orion import models, schemas
from prefect.orion.api import dependencies
from prefect.orion.database.dependencies import provide_database_interface
from prefect.orion.database.interface import OrionDBInterface
from prefect.orion.utilities.server import OrionRouter

router = OrionRouter(prefix="/block_documents", tags=["Block documents"])


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_block_document(
    block_document: schemas.actions.BlockDocumentCreate,
    response: Response,
    session: sa.orm.Session = Depends(dependencies.get_session),
    db: OrionDBInterface = Depends(provide_database_interface),
) -> schemas.core.BlockDocument:
    """
    Create a new block document.
    """
    try:
        new_block_document = await models.block_documents.create_block_document(
            session=session, block_document=block_document
        )
    except sa.exc.IntegrityError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Block already exists",
        )

    return new_block_document


@router.post("/filter")
async def read_block_documents(
    limit: int = dependencies.LimitBody(),
    block_documents: Optional[schemas.filters.BlockDocumentFilter] = None,
    block_types: Optional[schemas.filters.BlockTypeFilter] = None,
    block_schemas: Optional[schemas.filters.BlockSchemaFilter] = None,
    include_secrets: bool = Body(
        False, description="Whether to include sensitive values in the block document."
    ),
    offset: int = Body(0, ge=0),
    session: sa.orm.Session = Depends(dependencies.get_session),
) -> List[schemas.core.BlockDocument]:
    """
    Query for block documents.
    """
    result = await models.block_documents.read_block_documents(
        session=session,
        block_document_filter=block_documents,
        block_type_filter=block_types,
        block_schema_filter=block_schemas,
        include_secrets=include_secrets,
        offset=offset,
        limit=limit,
    )

    return result


@router.get("/{id:uuid}")
async def read_block_document_by_id(
    block_document_id: UUID = Path(
        ..., description="The block document id", alias="id"
    ),
    include_secrets: bool = Query(
        False, description="Whether to include sensitive values in the block document."
    ),
    session: sa.orm.Session = Depends(dependencies.get_session),
) -> schemas.core.BlockDocument:
    block_document = await models.block_documents.read_block_document_by_id(
        session=session,
        block_document_id=block_document_id,
        include_secrets=include_secrets,
    )
    if not block_document:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Block document not found")
    return block_document


@router.delete("/{id:uuid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_block_document(
    block_document_id: UUID = Path(
        ..., description="The block document id", alias="id"
    ),
    session: sa.orm.Session = Depends(dependencies.get_session),
):
    result = await models.block_documents.delete_block_document(
        session=session, block_document_id=block_document_id
    )
    if not result:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Block document not found"
        )


@router.patch("/{id:uuid}", status_code=status.HTTP_204_NO_CONTENT)
async def update_block_document_data(
    block_document: schemas.actions.BlockDocumentUpdate,
    block_document_id: UUID = Path(
        ..., description="The block document id", alias="id"
    ),
    session: sa.orm.Session = Depends(dependencies.get_session),
):
    try:
        result = await models.block_documents.update_block_document(
            session=session,
            block_document_id=block_document_id,
            block_document=block_document,
        )
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST)

    if not result:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Block document not found"
        )
