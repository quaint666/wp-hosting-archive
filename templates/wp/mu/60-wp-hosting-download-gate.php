<?php
/**
 * Managed by Ansible (templates/wp/mu/60-wp-hosting-download-gate.php). A must-use plugin of the hosting service, loaded from WPMU_PLUGIN_DIR (root-owned, read-only to PHP).
 *
 * The download of an All-in-One WP Migration export. The plugin's Download button is a plain link to /wp-content/uploads/ai1wm-backups/<name>.wpress, and an export is a whole site
 * (database and files), so nginx never serves that folder itself (templates/wp/site_nginx.conf.j2). It rewrites a request for a .wpress there to /index.php?wph_dl=<name>, and this file
 * answers it: an administrator gets an X-Accel-Redirect to the internal nginx location that serves the file; everyone else gets 403 (404 for a name that cannot be an export). PHP never
 * reads the file: nginx sends it, so a 200 MB export costs no PHP memory or time.
 * Only a site with WP_HOSTING_AI1WM_EXPORT in its wp-config.php (the site's ai1wm_export setting) registers anything. WP-CLI is the operator and is left alone.
 */
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}
if ( defined( 'WP_CLI' ) && WP_CLI ) {
	return;
}

if ( ! function_exists( 'wph_dl_decide' ) ) {
	/** array( status, internal nginx location or '' ) for a requested export name, from who is asking. Pure: the one place the rule lives. */
	function wph_dl_decide( $file, $logged_in, $is_admin ) {
		if ( ! is_string( $file ) || ! preg_match( '/^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.wpress\z/', $file ) ) {
			return array( 404, '' );
		}
		if ( ! $logged_in || ! $is_admin ) {
			return array( 403, '' );
		}
		return array( 200, '/__wph_ai1wm/' . $file );
	}

	/** Answer a rewritten download request. A missing file is nginx's 404 after the internal redirect. */
	function wph_dl_serve() {
		if ( ! isset( $_GET['wph_dl'] ) ) {
			return;
		}
		$answer = wph_dl_decide( $_GET['wph_dl'], is_user_logged_in(), current_user_can( 'manage_options' ) );
		nocache_headers();
		if ( 200 !== $answer[0] ) {
			status_header( $answer[0] );
			exit;
		}
		header( 'X-Accel-Redirect: ' . $answer[1] );
		exit;
	}

	if ( defined( 'WP_HOSTING_AI1WM_EXPORT' ) && WP_HOSTING_AI1WM_EXPORT ) {
		add_action( 'init', 'wph_dl_serve', 1 );
	}
}
