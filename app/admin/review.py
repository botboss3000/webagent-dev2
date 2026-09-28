"""
Admin endpoints for tool review and management.
"""

from fastapi import APIRouter, HTTPException
from typing import List, Any, Dict
from app.models.schemas import ToolRecord, ReviewToolRequest
from app.db import get_app_db
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

# Sentinel datetime used for built-in tool records (they have no real timestamps)
_EPOCH = datetime(2000, 1, 1)


def _builtin_tool_record(name: str, meta: Dict[str, Any]) -> dict:
    """Build a dict shaped like a ToolRecord row for a built-in tool."""
    return {
        "id": f"builtin:{name}",
        "name": name,
        "code": "",
        "description": "",
        "parameters": {},
        "language": "python",
        "status": "active",
        "created_by": "system",
        "source": "builtin",
        "stages": json.dumps(meta.get("stages", [])),
        "destructive": 1 if meta.get("destructive") else 0,
        "agent_types": json.dumps(meta.get("agent_types", [])),
        "created_at": _EPOCH,
        "updated_at": _EPOCH,
    }


@router.get("/tools")
async def list_all_tools():
    """
    Get all active tools: user-created skills (from DB) merged with
    built-in tools (from loader metadata). Each record includes
    stages, destructive, and agent_types for the loop visualizer.
    """
    try:
        from app.tools.loader import BUILTIN_TOOL_METADATA

        client = get_app_db().get_raw_client()
        response = (
            client.table("tools")
            .select("*")
            .eq("status", "active")
            .execute()
        )

        result = []
        db_names = set()
        for row in response.data or []:
            db_names.add(row["name"])
            r = dict(row)
            r["source"] = "skill"
            result.append(r)

        for name, meta in BUILTIN_TOOL_METADATA.items():
            if name not in db_names:
                result.append(_builtin_tool_record(name, meta))

        return result
    except Exception as e:
        logger.error(f"Error fetching tools: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tools/{tool_name}")
async def get_tool(tool_name: str):
    """
    Get a specific tool by name.
    """
    try:
        client = get_app_db().get_raw_client()
        response = (
            client.table("tools")
            .select("*")
            .eq("name", tool_name)
            .eq("status", "active")
            .limit(1)
            .execute()
        )

        if not response.data:
            raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

        return response.data[0]
    except Exception as e:
        logger.error(f"Error fetching tool {tool_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/tools/{tool_id}")
async def delete_tool(tool_id: str):
    """
    Soft-delete (deprecate) a tool.
    """
    try:
        client = get_app_db().get_raw_client()
        response = (
            client.table("tools")
            .update({"status": "deprecated", "updated_at": "now()"})
            .eq("id", tool_id)
            .execute()
        )

        if not response.data:
            raise HTTPException(status_code=404, detail="Tool not found")

        logger.info(f"Deprecated tool {tool_id}")
        return {"message": "Tool deprecated successfully"}
    except Exception as e:
        logger.error(f"Error deprecating tool {tool_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
