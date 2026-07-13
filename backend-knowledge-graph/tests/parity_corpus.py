"""Parser corpus — shared inputs that exercise every parser feature (routes, mounts,
DTOs, config, security, middleware, path converters, mixed frameworks, degraded files).

``CORPUS`` is single-file sources; ``PROJECTS`` is multi-file source maps (which also
exercise cross-file mounting / DTO resolution through the real pipeline). Used by the
golden served-output regression net (``test_served_golden``) and the parser unit +
determinism tests (``test_parser``). Kept dependency-free (plain strings).
"""

from __future__ import annotations

BOM = "﻿"

# --------------------------------------------------------------------- single file
CORPUS: list[tuple[str, str]] = [
    (
        "basic_methods",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/g')\n"
        "def g(): ...\n"
        "@app.post('/p')\n"
        "def p(): ...\n"
        "@app.put('/u')\n"
        "def u(): ...\n"
        "@app.patch('/pa')\n"
        "def pa(): ...\n"
        "@app.delete('/d')\n"
        "def d(): ...\n"
        "@app.head('/h')\n"
        "def h(): ...\n"
        "@app.options('/o')\n"
        "def o(): ...\n",
    ),
    (
        "router_path_kw",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get(path='/x')\n"
        "def x(): ...\n",
    ),
    (
        "api_route_methods",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.api_route('/multi', methods=['GET', 'POST'])\n"
        "def multi(): ...\n",
    ),
    (
        "websocket",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.websocket('/ws')\n"
        "def ws(): ...\n",
    ),
    (
        "response_model_and_tags",
        "from fastapi import APIRouter\n"
        "from app.schemas import UserOut\n"
        "router = APIRouter()\n"
        "@router.get('/u', response_model=UserOut, tags=['b', 'a', 'b'])\n"
        "def u(): ...\n",
    ),
    (
        "include_router_dotted",
        "from fastapi import FastAPI\n"
        "from app.routers import users\n"
        "app = FastAPI()\n"
        "app.include_router(users.router, prefix='/api/users', tags=['users'])\n",
    ),
    (
        "include_router_plain",
        "from fastapi import FastAPI, APIRouter\n"
        "app = FastAPI()\n"
        "router = APIRouter()\n"
        "app.include_router(router, prefix='/p')\n",
    ),
    (
        "middleware_add",
        "from fastapi import FastAPI\n"
        "from starlette.middleware.cors import CORSMiddleware\n"
        "app = FastAPI()\n"
        "app.add_middleware(CORSMiddleware)\n"
        "app.add_middleware(GZipMiddleware)\n",
    ),
    (
        "middleware_decorator",
        "from fastapi import FastAPI, Request\n"
        "app = FastAPI()\n"
        "@app.middleware('http')\n"
        "async def add_headers(request: Request, call_next): ...\n",
    ),
    (
        "params_depends",
        "from fastapi import APIRouter, Depends\n"
        "router = APIRouter()\n"
        "@router.get('/me')\n"
        "def me(token: str = Depends(auth)): ...\n",
    ),
    (
        "params_annotated_depends",
        "from fastapi import APIRouter, Depends\n"
        "from typing import Annotated\n"
        "router = APIRouter()\n"
        "@router.get('/me')\n"
        "def me(user: Annotated[User, Depends(current_user)]): ...\n",
    ),
    (
        "params_security",
        "from fastapi import APIRouter, Security\n"
        "router = APIRouter()\n"
        "@router.get('/s')\n"
        "def s(scopes: list = Security(get_scopes)): ...\n",
    ),
    (
        "params_path_query",
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/items/{item_id}')\n"
        "def get_item(item_id: int, q: str = 'x', limit: int = 10): ...\n",
    ),
    (
        "return_annotation",
        "from fastapi import APIRouter\n"
        "from app.schemas import UserOut\n"
        "router = APIRouter()\n"
        "@router.get('/u')\n"
        "def u() -> UserOut: ...\n",
    ),
    (
        "pydantic_models",
        "from pydantic import BaseModel, Field\n"
        "from typing import ClassVar, Optional\n"
        "class Base(BaseModel):\n"
        "    id: int\n"
        "class UserCreate(Base):\n"
        "    email: str\n"
        "    age: int = 0\n"
        "    tags: list = Field(default_factory=list)\n"
        "    name: str = Field(...)\n"
        "    nick: str = Field('anon')\n"
        "    role: str = Field(default='user')\n"
        "    _secret: str = 'x'\n"
        "    kind: ClassVar[str] = 'user'\n"
        "    model_config = {}\n"
        "    maybe: Optional[str] = None\n",
    ),
    (
        "basesettings",
        "from pydantic_settings import BaseSettings\n"
        "class Settings(BaseSettings):\n"
        "    debug: bool = False\n"
        "    db_url: str = 'sqlite://'\n",
    ),
    (
        "env_reads",
        "import os\n"
        "from os import getenv, environ\n"
        "A = os.getenv('A')\n"
        "B = os.getenv('B', 'default')\n"
        "C = os.environ['C']\n"
        "D = os.environ.get('D', 'd')\n"
        "E = getenv('E')\n"
        "F = environ['F']\n"
        "A2 = os.getenv('A', 'later-default')\n",
    ),
    (
        "security_schemes",
        "from fastapi.security import (\n"
        "    OAuth2PasswordBearer, HTTPBearer, HTTPBasic, HTTPDigest, APIKeyHeader\n"
        ")\n"
        "import fastapi\n"
        "oauth2 = OAuth2PasswordBearer(tokenUrl='token')\n"
        "bearer = HTTPBearer()\n"
        "basic = HTTPBasic()\n"
        "digest = HTTPDigest()\n"
        "api_key = APIKeyHeader(name='X-Key')\n"
        "dotted = fastapi.security.OAuth2AuthorizationCodeBearer(authorizationUrl='a', tokenUrl='t')\n",
    ),
    (
        "relative_imports",
        "from fastapi import APIRouter\n"
        "from . import deps\n"
        "from ..schemas import UserOut\n"
        "from .sub.mod import thing\n"
        "router = APIRouter()\n"
        "@router.get('/x', response_model=UserOut)\n"
        "def x(): ...\n",
    ),
    (
        "bom_prefix",
        BOM + "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/b')\n"
        "def b(): ...\n",
    ),
    (
        "no_framework_import",
        "app = something()\n@app.get('/x')\ndef x(): ...\n",
    ),
    (
        "comment_and_docstring_only",
        "'''module docstring'''\n# a comment\n",
    ),
    ("empty", ""),
    ("syntax_error", "from fastapi import FastAPI\ndef broken(:\n    ...\n"),
    # --- Flask ---
    (
        "flask_route_methods",
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "@app.route('/items', methods=['GET', 'POST'])\n"
        "def items(): ...\n"
        "@app.route('/plain')\n"
        "def plain(): ...\n",
    ),
    (
        "flask_method_shortcuts",
        "from flask import Blueprint\n"
        "bp = Blueprint('bp', __name__)\n"
        "@bp.get('/g')\n"
        "def g(): ...\n"
        "@bp.post('/p')\n"
        "def p(): ...\n",
    ),
    (
        "flask_path_converters",
        "from flask import Blueprint\n"
        "bp = Blueprint('bp', __name__)\n"
        "@bp.route('/u/<int:user_id>')\n"
        "def u(user_id): ...\n"
        "@bp.route('/files/<path:name>')\n"
        "def d(name): ...\n"
        "@bp.route('/x/<thing>')\n"
        "def x(thing): ...\n",
    ),
    (
        "flask_register_blueprint",
        "from flask import Flask\n"
        "from app.bp import bp\n"
        "app = Flask(__name__)\n"
        "app.register_blueprint(bp, url_prefix='/api/<int:v>')\n",
    ),
    (
        "flask_rule_kw",
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "@app.route(rule='/ruled', methods=['PUT'])\n"
        "def ruled(): ...\n",
    ),
    (
        "mixed_fastapi_flask",
        "from fastapi import FastAPI\n"
        "from flask import Flask\n"
        "app = FastAPI()\n"
        "@app.get('/x')\n"
        "def x(): ...\n"
        "@app.route('/y')\n"
        "def y(): ...\n",
    ),
]


# --------------------------------------------------------------------- multi file
_MAIN = (
    "from fastapi import FastAPI\n"
    "from starlette.middleware.cors import CORSMiddleware\n"
    "from app.routers import users\n"
    "app = FastAPI()\n"
    "app.add_middleware(CORSMiddleware)\n"
    "app.include_router(users.router, prefix='/api/users', tags=['users'])\n"
)
_USERS = (
    "from fastapi import APIRouter, Depends\n"
    "from app.schemas import UserCreate, UserOut\n"
    "from app.security import oauth2\n"
    "router = APIRouter()\n"
    "@router.get('/{user_id}', response_model=UserOut)\n"
    "def get_user(user_id: int): ...\n"
    "@router.post('/', response_model=UserOut)\n"
    "def create_user(payload: UserCreate, token: str = Depends(oauth2)): ...\n"
)
_SCHEMAS = (
    "from pydantic import BaseModel\n"
    "class UserBase(BaseModel):\n"
    "    id: int\n"
    "class UserCreate(UserBase):\n"
    "    email: str\n"
    "class UserOut(UserBase):\n"
    "    name: str\n"
)
_SECURITY = (
    "from fastapi.security import OAuth2PasswordBearer\n"
    "oauth2 = OAuth2PasswordBearer(tokenUrl='token')\n"
)

PROJECTS: list[tuple[str, dict[str, str]]] = [
    (
        "simple",
        {
            "app/__init__.py": "",
            "app/main.py": (
                "from fastapi import FastAPI\n"
                "from app.routers import users\n"
                "app = FastAPI()\n"
                "app.include_router(users.router, prefix='/api/users')\n"
            ),
            "app/routers/__init__.py": "",
            "app/routers/users.py": (
                "from fastapi import APIRouter\n"
                "router = APIRouter()\n"
                "@router.get('/{user_id}')\n"
                "def get_user(user_id: int): ...\n"
                "@router.post('/')\n"
                "def create_user(): ...\n"
            ),
        },
    ),
    (
        "full_depth",
        {
            "app/__init__.py": "",
            "app/main.py": _MAIN,
            "app/routers/__init__.py": "",
            "app/routers/users.py": _USERS,
            "app/schemas.py": _SCHEMAS,
            "app/security.py": _SECURITY,
        },
    ),
    (
        "flask_project",
        {
            "app/__init__.py": "",
            "app/main.py": (
                "from flask import Flask\n"
                "from app.views import bp\n"
                "app = Flask(__name__)\n"
                "app.register_blueprint(bp, url_prefix='/api')\n"
            ),
            "app/views.py": (
                "from flask import Blueprint\n"
                "bp = Blueprint('views', __name__)\n"
                "@bp.route('/items/<int:item_id>', methods=['GET', 'DELETE'])\n"
                "def item(item_id): ...\n"
                "@bp.get('/files/<path:name>')\n"
                "def download(name): ...\n"
            ),
        },
    ),
]
