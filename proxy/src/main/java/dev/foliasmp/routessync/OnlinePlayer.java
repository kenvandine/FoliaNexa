package dev.foliasmp.routessync;

/**
 * One player connected to one backend world, as reported to
 * {@code POST /api/v1/presence/report} (PLAN.md §7A). Deliberately not
 * {@code com.velocitypowered.api.proxy.Player} itself — keeps
 * {@link PresenceJson} testable with plain JUnit, same reasoning as
 * {@link PendingChatMessage} not wrapping a Velocity type.
 */
public record OnlinePlayer(String uuid, String username) {
}
