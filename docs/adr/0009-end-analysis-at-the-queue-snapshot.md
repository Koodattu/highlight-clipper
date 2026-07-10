# End analysis at the Queue Snapshot

Source Import creates a Source Recording before an Analysis Run begins; the Analysis Run ends when it commits a Queue Snapshot, while review commands and Exports remain independent append-only lifecycles. We reject one import-to-export job because human review can remain open indefinitely and rendering changes should not invalidate analysis, accepting explicit lifecycle identities in exchange for clear recovery, invalidation, and history.
