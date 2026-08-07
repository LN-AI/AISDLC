from fastapi import APIRouter
router = APIRouter()

@router.delete("/api/users/{user_id}")
def delete_user(user_id: str):
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"deleted": user_id}
