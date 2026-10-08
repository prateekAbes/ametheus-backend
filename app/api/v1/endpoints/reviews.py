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

async def cleanup_legacy_seed_reviews(db: AsyncIOMotorDatabase):
    """Purges any legacy placeholder/dummy seed reviews from the database."""
    try:
        await db.reviews.delete_many({"user_id": {"$regex": r"^seed_rev_"}})
    except Exception:
        pass

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
        user_name=doc.get("user_name", "Player"),
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

async def populate_review_usernames(db: AsyncIOMotorDatabase, docs: List[dict]) -> List[ReviewResponse]:
    if not docs:
        return []

    # Extract all unique user_ids
    user_ids = list({str(d.get("user_id")) for d in docs if d.get("user_id") and d.get("user_id") != "guest_player"})
    user_map: Dict[str, dict] = {}
    if user_ids:
        query = {
            "$or": [
                {"_id": {"$in": user_ids}},
                {"id": {"$in": user_ids}},
                {"clerk_user_id": {"$in": user_ids}},
            ]
        }
        cursor = db.users.find(query)
        matched_users = await cursor.to_list(length=1000)
        for u in matched_users:
            uname = u.get("username") or u.get("display_name")
            if uname:
                if u.get("_id"):
                    user_map[str(u["_id"])] = u
                if u.get("id"):
                    user_map[str(u["id"])] = u
                if u.get("clerk_user_id"):
                    user_map[str(u["clerk_user_id"])] = u

    results: List[ReviewResponse] = []
    for d in docs:
        uid = str(d.get("user_id", ""))
        u = user_map.get(uid)
        doc_copy = dict(d)
        if u:
            actual_name = u.get("username") or u.get("display_name")
            if actual_name:
                doc_copy["user_name"] = actual_name
            if u.get("avatar_url"):
                doc_copy["avatar_url"] = u.get("avatar_url")
        else:
            # Clean up generic legacy role names if present
            raw_name = str(doc_copy.get("user_name", "")).strip()
            if raw_name.lower() in ["contender", "contenders", "verified contender", "audit players"]:
                doc_copy["user_name"] = "Player"
        results.append(serialize_review_doc(doc_copy))
    return results

@router.get("", response_model=ReviewSummaryResponse)
async def list_public_reviews(
    category: Optional[str] = Query(None),
    db: AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Public listing of approved/featured reviews, with rating statistics and featured spotlight.
    """
    await cleanup_legacy_seed_reviews(db)

    match_filter: Dict[str, any] = {"status": {"$in": ["APPROVED", "FEATURED"]}}
    if category and category.upper() != "ALL":
        match_filter["category"] = category.upper()

    cursor = db.reviews.find(match_filter).sort("created_at", -1)
    review_docs = await cursor.to_list(length=100)

    reviews = await populate_review_usernames(db, review_docs)
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

    avg_rating = round(total_score / total_reviews, 1) if total_reviews > 0 else 0.0

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
    user_name = payload.user_name
    if current_user:
        # Prioritize authenticated user's actual username
        if not user_name or user_name.strip().lower() in ["contender", "verified contender", "player", "contenders", "audit players"]:
            user_name = current_user.username or current_user.display_name or "Player"
    else:
        user_name = user_name or "Player"

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

    # Create persistent user notification in DB
    target_uid = user_id
    if not target_uid or target_uid == "guest_player":
        if user_email and user_email != "contender@ametheus.gg":
            try:
                u_doc = await db.users.find_one({"email": user_email})
                if u_doc:
                    target_uid = u_doc.get("id") or str(u_doc.get("_id"))
            except Exception:
                pass
    if target_uid and not str(target_uid).startswith("guest"):
        try:
            from app.services.notification import create_notification
            await create_notification(
                db=db,
                user_id=str(target_uid),
                title="Review Submitted ⭐",
                message=f"Thank you for reviewing Ametheus! Your {payload.rating}-star review for {payload.category or 'PLATFORM'} has been submitted.",
                notification_type="SYSTEM",
                link="/#reviews",
            )
        except Exception:
            pass

    # Notify administrators of new review
    try:
        from app.services.notification import create_notification
        admins = await db.users.find({"$or": [{"role": {"$in": ["SUPER_ADMIN", "ORGANIZER"]}}, {"is_super_admin": True}]}).to_list(10)
        for admin in admins:
            admin_id = str(admin.get("id") or admin.get("_id"))
            await create_notification(
                db=db,
                user_id=admin_id,
                title=f"New Review: {payload.rating}★ from {user_name}",
                message=f"{sanitized_text[:120]}...",
                notification_type="SYSTEM",
                link="/admin",
            )
    except Exception:
        pass

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
    return await populate_review_usernames(db, docs)

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

    from app.models.base import get_utc_now
    update_doc: Dict[str, any] = {
        "status": st_upper,
        "is_featured": (st_upper == "FEATURED"),
        "updated_at": get_utc_now(),
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
    res_list = await populate_review_usernames(db, [refreshed])
    return res_list[0]

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
