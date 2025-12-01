"""Mycelia memory service implementation.

This module provides a concrete implementation of the MemoryServiceBase interface
that uses Mycelia as the backend for all memory operations.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import httpx

from ..base import MemoryEntry, MemoryServiceBase

memory_logger = logging.getLogger("memory_service")


class MyceliaMemoryService(MemoryServiceBase):
    """Memory service implementation using Mycelia backend.

    This class implements the MemoryServiceBase interface by delegating memory
    operations to a Mycelia server using JWT authentication from Friend-Lite.

    Args:
        api_url: Mycelia API endpoint URL
        timeout: Request timeout in seconds
        **kwargs: Additional configuration parameters
    """

    def __init__(
        self,
        api_url: str = "http://localhost:8080",
        timeout: int = 30,
        **kwargs
    ):
        """Initialize Mycelia memory service.

        Args:
            api_url: Mycelia API endpoint
            timeout: Request timeout in seconds
            **kwargs: Additional configuration parameters
        """
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.config = kwargs
        self._initialized = False
        self._client: Optional[httpx.AsyncClient] = None

        memory_logger.info(f"🍄 Initializing Mycelia memory service at {api_url}")

    async def initialize(self) -> None:
        """Initialize Mycelia client and verify connection."""
        try:
            # Initialize HTTP client
            self._client = httpx.AsyncClient(
                base_url=self.api_url,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"}
            )

            # Test connection directly (without calling test_connection to avoid recursion)
            try:
                response = await self._client.get("/health")
                if response.status_code != 200:
                    raise RuntimeError(f"Health check failed with status {response.status_code}")
            except httpx.HTTPError as e:
                raise RuntimeError(f"Failed to connect to Mycelia service: {e}")

            self._initialized = True
            memory_logger.info("✅ Mycelia memory service initialized successfully")

        except Exception as e:
            memory_logger.error(f"❌ Failed to initialize Mycelia service: {e}")
            raise RuntimeError(f"Mycelia initialization failed: {e}")

    async def _get_user_jwt(self, user_id: str, user_email: Optional[str] = None) -> str:
        """Get JWT token for a user (with optional user lookup).

        Args:
            user_id: User ID
            user_email: Optional user email (will lookup if not provided)

        Returns:
            JWT token string

        Raises:
            ValueError: If user not found
        """
        from advanced_omi_backend.auth import generate_jwt_for_user

        # If email not provided, lookup user
        if not user_email:
            from advanced_omi_backend.users import User
            user = await User.get(user_id)
            if not user:
                raise ValueError(f"User {user_id} not found")
            user_email = user.email

        return generate_jwt_for_user(user_id, user_email)

    @staticmethod
    def _extract_bson_id(raw_id: Any) -> str:
        """Extract ID from Mycelia BSON format {"$oid": "..."} or plain string."""
        if isinstance(raw_id, dict) and "$oid" in raw_id:
            return raw_id["$oid"]
        return str(raw_id)

    @staticmethod
    def _extract_bson_date(date_obj: Any) -> Any:
        """Extract date from Mycelia BSON format {"$date": "..."} or plain value."""
        if isinstance(date_obj, dict) and "$date" in date_obj:
            return date_obj["$date"]
        return date_obj

    def _mycelia_object_to_memory_entry(self, obj: Dict, user_id: str) -> MemoryEntry:
        """Convert Mycelia object to MemoryEntry.

        Args:
            obj: Mycelia object from API
            user_id: User ID for metadata

        Returns:
            MemoryEntry object
        """
        memory_id = self._extract_bson_id(obj.get("_id", ""))
        memory_content = obj.get("details", "")

        return MemoryEntry(
            id=memory_id,
            content=memory_content,
            metadata={
                "user_id": user_id,
                "name": obj.get("name", ""),
                "aliases": obj.get("aliases", []),
                "created_at": self._extract_bson_date(obj.get("createdAt")),
                "updated_at": self._extract_bson_date(obj.get("updatedAt")),
            },
            created_at=self._extract_bson_date(obj.get("createdAt"))
        )

    async def _call_resource(
        self,
        action: str,
        jwt_token: str,
        **params
    ) -> Dict[str, Any]:
        """Call Mycelia objects resource with JWT authentication.

        Args:
            action: Action to perform (create, list, get, delete, etc.)
            jwt_token: User's JWT token from Friend-Lite
            **params: Additional parameters for the action

        Returns:
            Response data from Mycelia

        Raises:
            RuntimeError: If API call fails
        """
        if not self._client:
            raise RuntimeError("Mycelia client not initialized")

        try:
            response = await self._client.post(
                "/api/resource/tech.mycelia.objects",
                json={"action": action, **params},
                headers={"Authorization": f"Bearer {jwt_token}"}
            )
            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as e:
            memory_logger.error(f"Mycelia API error: {e.response.status_code} - {e.response.text}")
            raise RuntimeError(f"Mycelia API error: {e.response.status_code}")
        except Exception as e:
            memory_logger.error(f"Failed to call Mycelia resource: {e}")
            raise RuntimeError(f"Mycelia API call failed: {e}")

    async def add_memory(
        self,
        transcript: str,
        client_id: str,
        source_id: str,
        user_id: str,
        user_email: str,
        allow_update: bool = False,
        db_helper: Any = None,
    ) -> Tuple[bool, List[str]]:
        """Add memories from transcript using Mycelia.

        Args:
            transcript: Raw transcript text to extract memories from
            client_id: Client identifier
            source_id: Unique identifier for the source (audio session, chat session, etc.)
            user_id: User identifier
            user_email: User email address
            allow_update: Whether to allow updating existing memories
            db_helper: Optional database helper for tracking relationships

        Returns:
            Tuple of (success: bool, created_memory_ids: List[str])
        """
        try:
            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id, user_email)

            # Create a Mycelia object for this memory
            # Memory content is stored in the 'details' field
            memory_preview = transcript[:50] + ("..." if len(transcript) > 50 else "")

            object_data = {
                "name": f"Memory: {memory_preview}",
                "details": transcript,
                "aliases": [source_id, client_id],  # Searchable by source or client
                "isPerson": False,
                "isPromise": False,
                "isEvent": False,
                "isRelationship": False,
                # Note: userId is auto-injected by Mycelia from JWT
            }

            result = await self._call_resource(
                action="create",
                jwt_token=jwt_token,
                object=object_data
            )

            memory_id = result.get("insertedId")
            if memory_id:
                memory_logger.info(f"✅ Created Mycelia memory object: {memory_id}")
                return (True, [memory_id])
            else:
                memory_logger.error("Failed to create Mycelia memory: no insertedId returned")
                return (False, [])

        except Exception as e:
            memory_logger.error(f"Failed to add memory via Mycelia: {e}")
            return (False, [])

    async def search_memories(
        self, query: str, user_id: str, limit: int = 10, score_threshold: float = 0.0
    ) -> List[MemoryEntry]:
        """Search memories using Mycelia semantic search.

        Args:
            query: Search query text
            user_id: User identifier to filter memories
            limit: Maximum number of results to return
            score_threshold: Minimum similarity score (0.0 = no threshold)

        Returns:
            List of matching MemoryEntry objects ordered by relevance
        """
        if not self._initialized:
            await self.initialize()

        try:
            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id)

            # Search using Mycelia's list action with searchTerm option
            result = await self._call_resource(
                action="list",
                jwt_token=jwt_token,
                filters={},  # Auto-scoped by userId in Mycelia
                options={
                    "searchTerm": query,
                    "limit": limit,
                    "sort": {"updatedAt": -1}  # Most recent first
                }
            )

            # Convert Mycelia objects to MemoryEntry objects
            memories = []
            for i, obj in enumerate(result):
                # Calculate a simple relevance score (0-1) based on position
                # (Mycelia doesn't provide semantic similarity scores yet)
                score = 1.0 - (i * 0.1)  # Decaying score
                if score < score_threshold:
                    continue

                entry = self._mycelia_object_to_memory_entry(obj, user_id)
                entry.score = score  # Override score
                memories.append(entry)

            return memories

        except Exception as e:
            memory_logger.error(f"Failed to search memories via Mycelia: {e}")
            return []

    async def get_all_memories(
        self, user_id: str, limit: int = 100
    ) -> List[MemoryEntry]:
        """Get all memories for a user from Mycelia.

        Args:
            user_id: User identifier
            limit: Maximum number of memories to return

        Returns:
            List of MemoryEntry objects for the user
        """
        if not self._initialized:
            await self.initialize()

        try:
            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id)

            # List all objects for this user (auto-scoped by Mycelia)
            result = await self._call_resource(
                action="list",
                jwt_token=jwt_token,
                filters={},  # Auto-scoped by userId
                options={
                    "limit": limit,
                    "sort": {"updatedAt": -1}  # Most recent first
                }
            )

            # Convert Mycelia objects to MemoryEntry objects
            memories = [self._mycelia_object_to_memory_entry(obj, user_id) for obj in result]
            return memories

        except Exception as e:
            memory_logger.error(f"Failed to get memories via Mycelia: {e}")
            return []

    async def count_memories(self, user_id: str) -> Optional[int]:
        """Count memories for a user.

        Args:
            user_id: User identifier

        Returns:
            Total count of memories for the user, or None if not supported
        """
        if not self._initialized:
            await self.initialize()

        try:
            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id)

            # Use Mycelia's mongo resource to count objects for this user
            if not self._client:
                raise RuntimeError("Mycelia client not initialized")

            response = await self._client.post(
                "/api/resource/tech.mycelia.mongo",
                json={
                    "action": "count",
                    "collection": "objects",
                    "query": {"userId": user_id}
                },
                headers={"Authorization": f"Bearer {jwt_token}"}
            )
            response.raise_for_status()
            return response.json()

        except Exception as e:
            memory_logger.error(f"Failed to count memories via Mycelia: {e}")
            return None

    async def delete_memory(self, memory_id: str, user_id: Optional[str] = None, user_email: Optional[str] = None) -> bool:
        """Delete a specific memory from Mycelia.

        Args:
            memory_id: Unique identifier of the memory to delete
            user_id: Optional user identifier for authentication
            user_email: Optional user email for authentication

        Returns:
            True if successfully deleted, False otherwise
        """
        try:
            # Need user credentials for JWT - if not provided, we can't delete
            if not user_id:
                memory_logger.error("User ID required for Mycelia delete operation")
                return False

            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id, user_email)

            # Delete the object (auto-scoped by userId in Mycelia)
            result = await self._call_resource(
                action="delete",
                jwt_token=jwt_token,
                id=memory_id
            )

            deleted_count = result.get("deletedCount", 0)
            if deleted_count > 0:
                memory_logger.info(f"✅ Deleted Mycelia memory object: {memory_id}")
                return True
            else:
                memory_logger.warning(f"No memory deleted with ID: {memory_id}")
                return False

        except Exception as e:
            memory_logger.error(f"Failed to delete memory via Mycelia: {e}")
            return False

    async def delete_all_user_memories(self, user_id: str) -> int:
        """Delete all memories for a user from Mycelia.

        Args:
            user_id: User identifier

        Returns:
            Number of memories that were deleted
        """
        try:
            # Generate JWT token for this user
            jwt_token = await self._get_user_jwt(user_id)

            # First, get all memory IDs for this user
            result = await self._call_resource(
                action="list",
                jwt_token=jwt_token,
                filters={},  # Auto-scoped by userId
                options={"limit": 10000}  # Large limit to get all
            )

            # Delete each memory individually
            deleted_count = 0
            for obj in result:
                memory_id = self._extract_bson_id(obj.get("_id", ""))
                if await self.delete_memory(memory_id, user_id):
                    deleted_count += 1

            memory_logger.info(f"✅ Deleted {deleted_count} Mycelia memories for user {user_id}")
            return deleted_count

        except Exception as e:
            memory_logger.error(f"Failed to delete user memories via Mycelia: {e}")
            return 0

    async def test_connection(self) -> bool:
        """Test connection to Mycelia service.

        Returns:
            True if connection is healthy, False otherwise
        """
        try:
            if not self._initialized:
                await self.initialize()

            if not self._client:
                return False

            # Test connection by hitting a lightweight endpoint
            response = await self._client.get("/health")
            return response.status_code == 200

        except Exception as e:
            memory_logger.error(f"Mycelia connection test failed: {e}")
            return False

    def shutdown(self) -> None:
        """Shutdown Mycelia client and cleanup resources."""
        memory_logger.info("Shutting down Mycelia memory service")
        if self._client:
            # Note: httpx AsyncClient should be closed in an async context
            # In practice, this will be called during shutdown so we log a warning
            memory_logger.warning("HTTP client should be closed with await client.aclose()")
        self._initialized = False
