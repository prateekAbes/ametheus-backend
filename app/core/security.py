import jwt
import urllib.request
import json
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta
from app.core.config import settings

ALGORITHM = "HS256"
_jwks_cache = {}

def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(days=7)
    to_encode.update({"exp": expire, "iat": now})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)

def format_pem_public_key(key_str: str) -> str:
    """Formats an arbitrary public key string or environment variable into standard PEM format."""
    cleaned = key_str.strip()
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith("'") and cleaned.endswith("'")):
        cleaned = cleaned[1:-1].strip()
    cleaned = cleaned.replace("\\n", "\n").strip()
    if not cleaned.startswith("-----BEGIN"):
        cleaned = f"-----BEGIN PUBLIC KEY-----\n{cleaned}\n-----END PUBLIC KEY-----"
    return cleaned

def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Decodes either internal HS256 tokens or Clerk RS256 JWT tokens.
    Cryptographically verifies RS256 tokens using CLERK_JWT_KEY or Clerk's official JWKS.
    Enforces signature verification, expiration check, and rejects deleted application tokens.
    """
    try:
        # First inspect header without verification to determine algorithm & kid
        unverified_header = jwt.get_unverified_header(token)
        alg = unverified_header.get("alg", "HS256")

        if alg == "HS256":
            return jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=["HS256"],
                options={"verify_signature": True, "verify_exp": True, "verify_nbf": True},
                leeway=60,
            )

        if alg == "RS256":
            # Inspect unverified claims to evaluate issuer and application identity
            unverified_payload = jwt.decode(token, options={"verify_signature": False})
            issuer = unverified_payload.get("iss", "").rstrip("/")

            # Reject stale references to the deleted Clerk application
            if "valid-goose-2403" in issuer or "valid-goose-2403" in token:
                print(f"[AUTH] Explicitly rejected token from deleted Clerk application: {issuer}")
                return None

            # Verify issuer is authorized
            is_valid_issuer = (
                not issuer
                or issuer == settings.CLERK_ISSUER.rstrip("/")
                or issuer.endswith(".clerk.accounts.dev")
                or "clerk." in issuer
            )
            if not is_valid_issuer:
                print(f"[AUTH] Rejected unauthorized token issuer: {issuer}")
                return None

            # 1. Networkless verification using CLERK_JWT_KEY if configured
            if settings.CLERK_JWT_KEY and settings.CLERK_JWT_KEY.strip():
                try:
                    pem_key = format_pem_public_key(settings.CLERK_JWT_KEY)
                    return jwt.decode(
                        token,
                        pem_key,
                        algorithms=["RS256"],
                        options={"verify_signature": True, "verify_exp": True, "verify_nbf": True, "verify_aud": False},
                        leeway=60,
                    )
                except jwt.ExpiredSignatureError:
                    print("[AUTH] Token expired")
                    return None
                except jwt.InvalidSignatureError:
                    print("[AUTH] CLERK_JWT_KEY signature mismatch, attempting JWKS fallback if kid present")
                except Exception as e:
                    print(f"[AUTH] CLERK_JWT_KEY decode error: {e}")

            # 2. Key-based JWKS verification
            kid = unverified_header.get("kid")
            if not kid or not issuer:
                return None

            global _jwks_cache
            if kid not in _jwks_cache:
                try:
                    jwks_url = f"{issuer}/.well-known/jwks.json"
                    req = urllib.request.Request(jwks_url, headers={"User-Agent": "Ametheus-Backend/1.0"})
                    with urllib.request.urlopen(req, timeout=5) as response:
                        jwks = json.loads(response.read().decode())
                        for key in jwks.get("keys", []):
                            _jwks_cache[key["kid"]] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
                except Exception as e:
                    print(f"[AUTH] Could not fetch Clerk JWKS from {jwks_url}: {e}")
                    return None

            if kid in _jwks_cache:
                public_key = _jwks_cache[kid]
                return jwt.decode(
                    token,
                    public_key,
                    algorithms=["RS256"],
                    options={"verify_signature": True, "verify_exp": True, "verify_nbf": True, "verify_aud": False},
                    leeway=60,
                )

            # Key not found in JWKS - reject!
            return None

        return None
    except jwt.ExpiredSignatureError:
        print("[AUTH] Token expired")
        return None
    except Exception as e:
        print(f"[AUTH] Token decode failure: {e}")
        return None
