package io.github.kenvandine.campuslobby;

/**
 * A single block to place, in coordinates relative to the scene origin.
 * {@code material} is a raw {@code org.bukkit.Material} enum name kept as
 * a plain String so this class carries no org.bukkit.* dependency — see
 * docs/plugin-dev/02-plugin-architecture.md §2.6 in the folia-server repo
 * for why scene/domain logic stays framework-free and unit-testable.
 */
public record BlockPlacement(int dx, int dy, int dz, String material) {
}
