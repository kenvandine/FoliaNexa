from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from folia_mgmt.auth import create_api_token, hash_password, require_admin
from folia_mgmt.db import get_session
from folia_mgmt.models import User, UserRole

router = APIRouter(prefix="/users", tags=["users"])


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: UserRole = UserRole.viewer


class UserResponse(BaseModel):
    id: int
    username: str
    role: str


class ApiTokenResponse(BaseModel):
    token: str


@router.post("", response_model=UserResponse, dependencies=[Depends(require_admin)])
def create_user(body: CreateUserRequest, session: Session = Depends(get_session)) -> UserResponse:
    if session.exec(select(User).where(User.username == body.username)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, f"user '{body.username}' already exists")
    user = User(username=body.username, password_hash=hash_password(body.password), role=body.role)
    session.add(user)
    session.commit()
    session.refresh(user)
    return UserResponse(id=user.id, username=user.username, role=user.role.value)


@router.get("", response_model=list[UserResponse], dependencies=[Depends(require_admin)])
def list_users(session: Session = Depends(get_session)) -> list[UserResponse]:
    users = session.exec(select(User)).all()
    return [UserResponse(id=u.id, username=u.username, role=u.role.value) for u in users]


@router.post("/{username}/api-token", response_model=ApiTokenResponse, dependencies=[Depends(require_admin)])
def issue_api_token(username: str, session: Session = Depends(get_session)) -> ApiTokenResponse:
    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such user '{username}'")
    return ApiTokenResponse(token=create_api_token(session, user))
