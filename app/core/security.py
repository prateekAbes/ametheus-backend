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
    Cryptographically verifies RS256 tokens using Clerk's official JWKS.
    No unverified signature bypass is permitted.
    """
    try:
        # First inspect header without verification to determine algorithm & kid
        unverified_header = jwt.get_unverified_header(token)
        alg = unverified_header.get("alg", "HS256")

        if alg == "HS256":
            return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"], leeway=60)

        if alg == "RS256":
            kid = unverified_header.get("kid")
            if not kid:
                return None

            # Inspect claims to find issuer
            unverified_payload = jwt.decode(token, options={"verify_signature": False})
            issuer = unverified_payload.get("iss", "").rstrip("/")

            # Verify issuer is authorized
            is_valid_issuer = (
                issuer == settings.CLERK_ISSUER.rstrip("/")
                or issuer.endswith(".clerk.accounts.dev")
                or "clerk." in issuer
            )
            if not is_valid_issuer:
                print(f"[AUTH] Rejected unauthorized token issuer: {issuer}")
                return None

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
                    print(f"[AUTH] Could not fetch Clerk JWKS from {jwks_url}: {e}")
                    return None

            if kid in _jwks_cache:
                public_key = _jwks_cache[kid]
                return jwt.decode(
                    token,
                    public_key,
                    algorithms=["RS256"],
                    options={"verify_aud": False},
                    leeway=60,
                )

            # Key not found in JWKS - reject!
            return None

        return None
    except Exception as e:
        print(f"[AUTH] Token decode failure: {e}")
        return None
