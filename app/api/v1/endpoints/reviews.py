from typing import List, Optional, Dict
from datetime import datetime
import re
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, get_current_user_optional, require_organizer, require_super_admin
from app.models.user import User
from app.models.review import Review
from app.schemas import ReviewResponse, ReviewCreate, ReviewStatusUpdate, ReviewSummaryResponse
from app.services.audit import record_audit_log

router = APIRouter()
admin_router = APIRouter()

DEFAULT_SEED_REVIEWS = [
    {
        "user_id": "seed_rev_01",
        "user_name": "Aman 'Shadow' Sharma",
        "user_email": "aman.shadow@ametheus.gg",
        "avatar_url": "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=150&auto=format&fit=crop",
        "rating": 5,
        "review_text": "The automated bracket progression and instant lobby credential delivery make Ametheus by far the cleanest tournament platform in Indian esports. Payouts via UPI took less than 10 minutes after grand finals.",
        "category": "TOURNAMENTS",
        "status": "FEATURED",
        "is_verified": True,
        "is_featured": True,
    },
    {
        "user_id": "seed_rev_02",
        "user_name": "Rohan 'Viper' Verma",
        "user_email": "rohan.viper@ametheus.gg",
        "avatar_url": "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=150&auto=format&fit=crop",
        "rating": 5,
        "review_text": "Our squad competed in the Free Fire Max championship. Zero server lag, instant referee responses, and the WhatsApp group community links kept our teammates synced before every round.",
        "category": "MATCHMAKING",
        "status": "APPROVED",
        "is_verified": True,
        "is_featured": False,
    },
    {
        "user_id": "seed_rev_03",
        "user_name": "Priya 'Valkyrie' Patel",
        "user_email": "priya.valkyrie@ametheus.gg",
        "avatar_url": "https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=150&auto=format&fit=crop",
        "rating": 5,
        "review_text": "The anti-cheat discipline and automated score verifications are top-tier. As a team captain, getting real-time Brevo email alerts for schedule adjustments made match management effortless.",
        "category": "PLATFORM",
        "status": "APPROVED",
        "is_verified": True,
        "is_featured": False,
    },
]

async def seed_reviews_if_needed(db: AsyncIOMotorDatabase):
    count = await db.reviews.count_documents({})
    if count == 0:
        for sr in DEFAULT_SEED_REVIEWS:
            r = Review(**sr)
            await db.reviews.insert_one(r.to_mongo())

def sanitize_review_text(text: str) -> str:
    cleaned = re.sub(r'<[^>]*?>', '', text)
    return cleaned.strip()

def serialize_review_doc(doc: dict) -> ReviewResponse:
    rid = str(doc.get("_id", doc.get("id")))
    created_at_str = None
    ca = doc.get("created_at")
    if isinstance(ca, datetime):
        created_at_str = ca.isoformat()
    elif ca:
        created_at_str = str(ca)

    return ReviewResponse(
        id=rid,
        user_id=str(doc.get("user_id", "")),
        user_name=doc.get("user_name", "Contender"),
        avatar_url=doc.get("avatar_url"),
        rating=int(doc.get("rating", 5)),
        review_text=doc.get("review_text", ""),
        category=doc.get("category", "PLATFORM"),
        status=doc.get("status", "PENDING"),
        is_verified=bool(doc.get("is_verified", True)),
        is_featured=bool(doc.get("is_featured", False)),
        admin_notes=doc.get("admin_notes"),
        created_at=created_at_str,
    )

@router.get("", response_model=ReviewSummaryResponse)
async def list_public_reviews(
    category: Optional[str] = Query(None),
    db: AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Public listing of approved/featured reviews, with rating statistics and featured spotlight.
    """
    await seed_reviews_if_needed(db)

    match_filter: Dict[str, any] = {"status": {"$in": ["APPROVED", "FEATURED"]}}
    if category and category.upper() != "ALL":
        match_filter["category"] = category.upper()

    cursor = db.reviews.find(match_filter).sort("created_at", -1)
    review_docs = await cursor.to_list(length=100)

    reviews = [serialize_review_doc(d) for d in review_docs]
    total_reviews = len(reviews)

    rating_dist = {str(i): 0 for i in range(1, 6)}
    total_score = 0
    featured_review: Optional[ReviewResponse] = None

    for r in reviews:
        score = max(1, min(5, r.rating))
        rating_dist[str(score)] = rating_dist.get(str(score), 0) + 1
        total_score += score
        if r.is_featured and not featured_review:
            featured_review = r

    if not featured_review and reviews:
        featured_review = reviews[0]

    avg_rating = round(total_score / total_reviews, 1) if total_reviews > 0 else 5.0

    return ReviewSummaryResponse(
        average_rating=avg_rating,
        total_reviews=total_reviews,
        rating_distribution=rating_dist,
        reviews=reviews,
        featured_review=featured_review,
    )

@router.post("", response_model=ReviewResponse, status_code=status.HTTP_201_CREATED)
async def submit_review(
    payload: ReviewCreate,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Submit a platform or tournament experience review.
    Text is sanitized; rating must be 1 to 5.
    """
    sanitized_text = sanitize_review_text(payload.review_text)
    if len(sanitized_text) < 10:
        raise HTTPException(status_code=400, detail="Review text must be at least 10 characters long.")

    user_id = current_user.id if current_user else "guest_player"
    user_name = payload.user_name or (current_user.display_name or current_user.username if current_user else "Verified Contender")
    user_email = current_user.email if current_user else "contender@ametheus.gg"
    avatar_url = payload.avatar_url or (current_user.avatar_url if current_user else None)

    review = Review(
        user_id=user_id,
        user_name=user_name,
        user_email=user_email,
        avatar_url=avatar_url,
        rating=max(1, min(5, payload.rating)),
        review_text=sanitized_text,
        category=(payload.category or "PLATFORM").upper(),
        status="APPROVED",  # Default approved with moderation ability
        is_verified=True if current_user else False,
        is_featured=False,
    )
    await db.reviews.insert_one(review.to_mongo())

    return serialize_review_doc(review.to_mongo())

# Admin Operations
@admin_router.get("", response_model=List[ReviewResponse])
async def list_admin_reviews(
    status_filter: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    await seed_reviews_if_needed(db)
    query: Dict[str, any] = {}
    if status_filter and status_filter.upper() != "ALL":
        query["status"] = status_filter.upper()
    if search:
        query["$or"] = [
            {"user_name": {"$regex": search, "$options": "i"}},
            {"review_text": {"$regex": search, "$options": "i"}},
            {"category": {"$regex": search, "$options": "i"}},
        ]

    cursor = db.reviews.find(query).sort("created_at", -1)
    docs = await cursor.to_list(length=200)
    return [serialize_review_doc(d) for d in docs]

@admin_router.patch("/{id}/status", response_model=ReviewResponse)
async def update_review_status(
    id: str,
    payload: ReviewStatusUpdate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    doc = await db.reviews.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Review not found")

    st_upper = payload.status.upper()
    if st_upper not in ["APPROVED", "FEATURED", "HIDDEN", "PENDING"]:
        raise HTTPException(status_code=400, detail="Invalid review status.")

    update_doc: Dict[str, any] = {
        "status": st_upper,
        "is_featured": (st_upper == "FEATURED"),
        "updated_at": datetime.utcnow(),
    }
    if payload.admin_notes is not None:
        update_doc["admin_notes"] = payload.admin_notes

    await db.reviews.update_one({"_id": doc["_id"]}, {"$set": update_doc})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="MODERATE_REVIEW",
        target=f"Review {id} by {doc.get('user_name')}",
        details=f"Status set to {st_upper}",
    )

    refreshed = await db.reviews.find_one({"_id": doc["_id"]})
    return serialize_review_doc(refreshed)

@admin_router.delete("/{id}")
async def delete_review(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    doc = await db.reviews.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Review not found")

    await db.reviews.delete_one({"_id": doc["_id"]})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="DELETE_REVIEW",
        target=f"Review {id}",
        details=f"Deleted review by {doc.get('user_name')}",
    )

    return {"status": "success", "message": "Review deleted successfully."}
