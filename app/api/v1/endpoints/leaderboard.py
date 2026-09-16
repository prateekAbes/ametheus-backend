from typing import List
from fastapi import APIRouter
from app.schemas import LeaderboardEntryResponse

router = APIRouter()

MOCK_LEADERBOARD = [
  {
    "rank": 1,
    "id": "lb-solaris-x",
    "name": "Solaris X",
    "tag": "SLX",
    "type": "team",
    "game_slug": "valorant",
    "game_name": "Valorant",
    "captain": "@vortex_lead",
    "logo_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuDdPWn2ez9ufUiy3oobQMiyzKWPPQYVyngmyTfKqJMWwfmrnUALrqljOu3fdZn-93iR00dO8abRLetxpR3H_YY3Lw0kKtOiie2zXvcvZIFoaGNsiSwx85D3D6OxG0gHpFRiib2Ya4XvFEph7nC919c8zczdq9v32ddvS6lpbCcpe-vwqwPx1OMgEU1xRwlEmm5SIE6Rj7g-L0YKOPoAoiiNPDGGv944Lu6ZDoOPAhH3_SduIY6JiOlN",
    "win_rate": 94.2,
    "matches_played": 45,
    "wins": 42,
    "losses": 3,
    "rating": 4890,
    "earnings": 185000,
    "form": ["W", "W", "W", "W", "W"],
    "is_verified": True,
    "category": "pc",
  },
  {
    "rank": 2,
    "id": "lb-titan-clash",
    "name": "Titan Clash",
    "tag": "TC-ELITE",
    "type": "team",
    "game_slug": "counter-strike-2",
    "game_name": "Counter-Strike 2",
    "captain": "@reaper_cs",
    "logo_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuDMYikVhkT2cPuCcT6iHyvC8KRrGxi2uWjy_Yg4PRywCpZ0Qk_XuBVy-SRnVARKIhqdj-kgUgfYlRjv_KTabBT194pEvMHjgNWeddHSPbu6P_N7NZD2B8VX7etUUzxmFCcI_NOLjS7LndnhoB8Te6UWYv5FexbsBx1329LlvMT7RFsiQ8_LX9hHuWmyhH0syOHCVlI_t-LXya3oFvZnd1NPJyQz_IZjYv6PmnX97rPssU0Rf0xwleEz",
    "win_rate": 89.1,
    "matches_played": 43,
    "wins": 38,
    "losses": 5,
    "rating": 3420,
    "earnings": 120000,
    "form": ["W", "W", "L", "W", "W"],
    "is_verified": True,
    "category": "pc",
  },
  {
    "rank": 3,
    "id": "lb-soul-cadets",
    "name": "Soul Cadets",
    "tag": "SOUL",
    "type": "team",
    "game_slug": "bgmi",
    "game_name": "BGMI",
    "captain": "@ronin_bgmi",
    "logo_url": "https://lh3.googleusercontent.com/aida-public/AB6AXuDJyotics78wQBHUOgYaMmCce9e_DLqZgHGyJP2jKj3_xhVWvP8PhkqLa01gBm4tie9z2jbwUOwI2TSDd-1RCv9Yr1j_bUg4AMrX7V7fqg8mzDAOcz4eQg4ZNSNOcUWqG1PlAPqo4-G1Rvph-2LGIaj1bgttW-RenPdreK7vomeC4gDL4GXEwsIEcE6wVnBzjS3SFx5cbwTz8brPBmRLw8Ch6yloRusSeTA9UCmJGJDLMCmfMfSyONn",
    "win_rate": 86.4,
    "matches_played": 37,
    "wins": 32,
    "losses": 5,
    "rating": 2980,
    "earnings": 95000,
    "form": ["W", "L", "W", "W", "W"],
    "is_verified": True,
    "category": "mobile",
  },
]

@router.get("", response_model=List[LeaderboardEntryResponse])
async def get_leaderboard():
    return [LeaderboardEntryResponse(**e) for e in MOCK_LEADERBOARD]
