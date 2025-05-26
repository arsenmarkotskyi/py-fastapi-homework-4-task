from fastapi import APIRouter, Depends, status, Header, Path, HTTPException, Form
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from config import get_jwt_auth_manager, get_s3_storage_client
from exceptions import (
    S3ConnectionError,
    S3FileUploadError,
    TokenExpiredError,
    InvalidTokenError
)
from schemas.profiles import ProfileRequestSchema, ProfileResponseSchema
from database import (
    get_db,
    UserModel, UserProfileModel, UserGroupModel
)
from security.interfaces import JWTAuthManagerInterface
from storages import S3StorageInterface

router = APIRouter()

# Write your code here

@router.post(
    "/users/{user_id}/profile/",
    response_model=ProfileResponseSchema,
    status_code=status.HTTP_201_CREATED
)
async def create_profile(
    profile_data: Annotated[ProfileRequestSchema, Form()],
    user_id: Annotated[int, Path()],
    header: Annotated[str | None, Header(alias="authorization")] = None,
    jwt_manager: JWTAuthManagerInterface = Depends(get_jwt_auth_manager),
    db: AsyncSession = Depends(get_db),
    s3_client: S3StorageInterface = Depends(get_s3_storage_client)
):
    query = select(UserModel).options(joinedload(UserModel.profile)).where(UserModel.id == user_id)
    user = (await db.execute(query)).scalars().first()

    if not header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header is missing"
        )
    splited_header = header.split()
    if len(splited_header) != 2 or splited_header[0] != "Bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Expected 'Bearer <token>'"
        )
    token = splited_header[1]
    try:
        token_data = jwt_manager.decode_access_token(token)
    except TokenExpiredError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired."
        )
    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Expected 'Bearer <token>'"
        )

    token_user_id = token_data.get("user_id")

    if token_user_id != user_id:
        query = select(UserGroupModel.name).join(UserModel).where(UserModel.id == token_user_id)
        token_user_group_name = (await db.execute(query)).scalar_one_or_none()
        print(f"{token_user_group_name=}")
        if token_user_group_name != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have permission to edit this profile."
            )

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or not active."
        )

    if user.profile:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already has a profile."
        )

    try:

        new_filename = f"avatars/{user_id}_avatar.jpg"
        print(f"{new_filename=}")
        file_bytes = await profile_data.avatar.read()
        print(f"{file_bytes=}")
        await s3_client.upload_file(
            file_name=new_filename,
            file_data=file_bytes
        )
    except S3FileUploadError as e:
        print(e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload avatar. Please try again later."
        )
    except S3ConnectionError as e:
        print(e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload avatar. Please try again later."
        )

    file_url = await s3_client.get_file_url(file_name=new_filename)
    print(f"{file_url=}")
    profile_dict = profile_data.model_dump(exclude_unset=True)
    profile_dict["avatar"] = new_filename
    profile_dict["user_id"] = user_id
    profile = UserProfileModel(**profile_dict)
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    data = ProfileResponseSchema.model_validate(profile, from_attributes=True)
    data.avatar = file_url
    return data