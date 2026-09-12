# Church Management System - Backend

This is the backend API for the Church Management System. It is a modern, high-performance web service built with Python and FastAPI, designed to decouple the frontend from backend logic, providing a robust REST API.

## 🚀 Tech Stack

- **Framework:** [FastAPI](https://fastapi.tiangolo.com/)
- **Language:** Python 3.12+
- **Database:** PostgreSQL (Hosted on [Neon](https://neon.tech/))
- **ORM:** [SQLAlchemy 2.0](https://www.sqlalchemy.org/) (Asynchronous)
- **Migrations:** [Alembic](https://alembic.sqlalchemy.org/en/latest/)
- **Authentication:** JWT (JSON Web Tokens) with Refresh Token Rotation
- **Password Hashing:** Argon2 (`pwdlib`)
- **Package Manager:** [uv](https://github.com/astral-sh/uv)

## 📋 Prerequisites

Before you begin, ensure you have the following installed:
- [Python 3.12](https://www.python.org/downloads/) or higher
- [uv](https://github.com/astral-sh/uv) (Extremely fast Python package installer and resolver)
- A PostgreSQL Database (e.g., Neon Postgres)

## 🛠️ Setup & Installation

**1. Navigate to the backend directory:**
```bash
cd backend
```

**2. Setup Virtual Environment & Install Dependencies:**
Using `uv`, you can easily sync your environment based on the `pyproject.toml` file.
```bash
uv sync
```
*This will automatically create a `.venv` directory and install all required dependencies.*

**3. Environment Variables:**
Create a `.env` file in the `backend/` root directory and add the following configurations:
```env
# Application Settings
PROJECT_NAME="Church Management System API"
VERSION="1.0.0"

# Database Configuration (Neon PostgreSQL)
DATABASE_URL="postgresql+asyncpg://<username>:<password>@<host>/<database>?ssl=require"

# Security Settings
SECRET_KEY="your-super-secret-key-change-this-in-production"
ALGORITHM="HS256"
ACCESS_TOKEN_EXPIRE_MINUTES=30
```

**4. Database Migrations:**
Initialize the database tables by running the Alembic migrations. This will apply all schemas to your Neon PostgreSQL instance:
```bash
uv run alembic upgrade head
```

**5. Seed the Superadmin Account:**
To gain initial access to the system, you must seed the superadmin credentials into the database:
```bash
uv run python seed_superadmin.py
```
*Default Credentials: `superadmin@churchhub.com` / `superadmin123`*

## 🚀 Running the Server

To start the FastAPI server in development mode with live reload:
```bash
uv run uvicorn app.main:app --reload
```
The API will be available at: `http://127.0.0.1:8000`

## 📚 API Documentation

FastAPI automatically generates interactive API documentation. Once the server is running, you can access:
- **Swagger UI:** `http://127.0.0.1:8000/docs`
- **ReDoc:** `http://127.0.0.1:8000/redoc`

## 🔒 Authentication Flow

This backend uses a secure, decoupled authentication architecture:
1. `POST /api/v1/auth/login`: Returns an `access_token` (short-lived) and a `refresh_token` (long-lived).
2. The frontend attaches the `access_token` to the `Authorization: Bearer <token>` header.
3. If the access token expires, the frontend automatically hits `POST /api/v1/auth/refresh` to obtain a new token pair using the valid refresh token.
4. All timestamps in the database are stored as `TIMESTAMP WITH TIME ZONE` (UTC) to ensure global compatibility.