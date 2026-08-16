package dev.foliasmp.routessync;

/**
 * One queued Discord->game chat message from {@code GET /api/v1/chat/pending}
 * (PLAN.md §16). {@code world} is null for a server-wide-channel message —
 * broadcast to every connected player rather than just one world's.
 */
public record PendingChatMessage(String world, String author, String message) {
}
