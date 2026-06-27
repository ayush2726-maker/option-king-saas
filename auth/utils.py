import jwt
import bcrypt
import os
from datetime import datetime, timedelta
from cryptography.fernet import Fernet

SECRET_KEY = os.getenv("SECRET_KEY", "option-king-super-secret-change-in-production-2024")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

# Encryption key for broker credentials
ENCRYPT_KEY = os.getenv("ENCRYPT_KEY", Fernet.generate_key().decode())
fernet = Fernet(ENCRYPT_KEY.encode() if isinstance(ENCRYPT_KEY, str) else ENCRYPT_KEY)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_access_token(user_id: int, email: str) -> str:
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
        "iat": datetime.utcnow()
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise ValueError("Token expired — please login again")
    except jwt.InvalidTokenError:
        raise ValueError("Invalid token")


def encrypt_credential(value: str) -> str:
    """Encrypt sensitive broker credentials"""
    return fernet.encrypt(value.encode()).decode()


def decrypt_credential(value: str) -> str:
    """Decrypt broker credentials"""
    return fernet.decrypt(value.encode()).decode()
