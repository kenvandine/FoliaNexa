package io.github.kenvandine.campuslobby;

import org.bukkit.plugin.java.JavaPlugin;

/**
 * Procedurally decorates a FoliaNexa lobby world into an NC State
 * Wolfpack-themed scene. Keep this class thin — wiring only. The actual
 * generation logic lives in CampusScene (plain Java, no org.bukkit.*
 * imports, unit-tested) and SceneBuilder (the only class that touches
 * game state).
 */
public final class CampusLobbyPlugin extends JavaPlugin {

    @Override
    public void onEnable() {
        saveDefaultConfig();
        getServer().getPluginManager().registerEvents(new CampusLobbyListener(this), this);
        getCommand("campuslobby").setExecutor(new CampusLobbyCommand(this));
    }

    @Override
    public void onDisable() {
        // Paper cancels tasks scheduled through its own schedulers
        // (Bukkit.getRegionScheduler()/getGlobalRegionScheduler()/
        // getAsyncScheduler()) automatically on plugin disable — nothing
        // else to clean up here.
    }
}
