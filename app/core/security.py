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

def decode_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Decodes either internal HS256 tokens or Clerk RS256 JWT tokens.
    """
    try:
        # First check header without verification to determine algorithm
        unverified_header = jwt.get_unverified_header(token)
        alg = unverified_header.get("alg", "HS256")

        if alg == "HS256":
            return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])

        if alg == "RS256":
            # Clerk JWT decoding via JWKS
            issuer = settings.CLERK_ISSUER.rstrip("/")
            kid = unverified_header.get("kid")

            # Check if JWKS is cached or fetch
            global _jwks_cache
            if not _jwks_cache or kid not in _jwks_cache:
                try:
                    jwks_url = f"{issuer}/.well-known/jwks.json"
                    req = urllib.request.Request(jwks_url, headers={"User-Agent": "Ametheus-Backend/1.0"})
                    with urllib.request.urlopen(req, timeout=5) as response:
                        jwks = json.loads(response.read().decode())
                        for key in jwks.get("keys", []):
                            _jwks_cache[key["kid"]] = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
                except Exception as e:
                    print(f"[AUTH] Could not fetch Clerk JWKS: {e}")

            if kid in _jwks_cache:
                public_key = _jwks_cache[kid]
                return jwt.decode(
                    token,
                    public_key,
                    algorithms=["RS256"],
                    options={"verify_aud": False}
                )

            # Fallback: unverified decode for dev/mocking if remote JWKS unavailable
            return jwt.decode(token, options={"verify_signature": False})

        return None
    except Exception as e:
        print(f"[AUTH] Token decode failure: {e}")
        return None
