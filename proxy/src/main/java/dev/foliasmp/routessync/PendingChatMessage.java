package dev.foliasmp.routessync;

/**
 * One queued message from {@code GET /api/v1/chat/pending} (PLAN.md §16).
 * {@code world} is null for a server-wide message — broadcast to every
 * connected player rather than just one world's. {@code kind} is
 * {@code "discord"} for a Discord->game relayed chat line (the original,
 * only kind this endpoint ever produced) or {@code "broadcast"} for an
 * operator-authored announcement queued via {@code POST
 * /api/v1/chat/broadcast} — see FoliaRoutesSyncPlugin.deliverPendingChat
 * for how the two render differently.
 */
public record PendingChatMessage(String world, String author, String message, String kind) {
}
