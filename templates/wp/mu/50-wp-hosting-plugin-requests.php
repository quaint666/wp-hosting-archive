<?php
/**
 * Managed by Ansible (templates/wp/mu/50-wp-hosting-plugin-requests.php). A must-use plugin of the hosting service, loaded from WPMU_PLUGIN_DIR (root-owned, read-only to PHP).
 *
 * "Install" in wp-admin becomes a request to the operator. A site cannot install code (wp-config sets DISALLOW_FILE_MODS and the php container mounts the code read-only), so Plugins > Add New is
 * switched off by WordPress. This file switches the screen back on for administrators and catches the one thing the screen is for:
 *  - map_meta_cap gives administrators the install_plugins capability back, so the menu entry, the search, the featured list and the Install buttons appear;
 *  - an Install press (the button's ajax call, and the plain link the plugin details window uses) is answered here, BEFORE WordPress's own handler, by writing a request into the option
 *    wph_plugin_requests: a wordpress.org slug, who asked, and when. Nothing is installed. The code mount being read-only is the second lock if this file ever failed to catch a press;
 *  - uploading a zip is refused (and its button hidden); it is exactly what the hosting policy forbids;
 *  - the operator's side (the host reads the option with WP-CLI, asks for approval, installs from wordpress.org by slug) writes its answers into wph_plugin_request_results, which only the host
 *    writes, so the two sides never write the same option. The administrator sees each request and its state in a notice on the Plugins pages.
 * Everything in the option is the SITE's word, so the host checks every value again. The limit on requests waiting at once is WP_HOSTING_PLUGIN_REQUESTS_MAX in wp-config.php (0 turns all of
 * this off and leaves WordPress as it was: no Add New). WP-CLI is the operator and is left alone.
 */
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}
if ( defined( 'WP_CLI' ) && WP_CLI ) {
	return;
}

if ( ! function_exists( 'wph_pr_max' ) ) {
	/** Requests that may wait at once; 0 is off. */
	function wph_pr_max() {
		return defined( 'WP_HOSTING_PLUGIN_REQUESTS_MAX' ) ? max( 0, (int) constant( 'WP_HOSTING_PLUGIN_REQUESTS_MAX' ) ) : 0;
	}

	/** A wordpress.org plugin slug, or '' for anything else (no dots, slashes, colons or capitals: no file name, path or address can pass). */
	function wph_pr_slug( $value ) {
		$value = is_string( $value ) ? $value : '';
		return preg_match( '/^[a-z0-9][a-z0-9-]{0,99}\z/', $value ) ? $value : '';
	}

	/** A list of requests as the option holds it, with every entry that is not well formed left out. */
	function wph_pr_requests() {
		$out = array();
		foreach ( (array) get_option( 'wph_plugin_requests', array() ) as $entry ) {
			if ( ! is_array( $entry ) || ! isset( $entry['id'], $entry['slug'], $entry['t'] ) || ! is_string( $entry['id'] ) || ! preg_match( '/^[a-f0-9]{8,24}\z/', $entry['id'] ) ) {
				continue;
			}
			if ( '' === wph_pr_slug( $entry['slug'] ) || ! is_int( $entry['t'] ) ) {
				continue;
			}
			$out[] = array(
				'id'   => $entry['id'],
				'slug' => $entry['slug'],
				'user' => isset( $entry['user'] ) && is_string( $entry['user'] ) ? substr( $entry['user'], 0, 60 ) : '',
				't'    => $entry['t'],
			);
		}
		return $out;
	}

	/** What the host answered, by request id: only a known status, a short note. (PHP turns an id made only of digits into an integer key; looking it up with the string id still finds it.) */
	function wph_pr_results() {
		$out = array();
		foreach ( (array) get_option( 'wph_plugin_request_results', array() ) as $id => $row ) {
			if ( is_array( $row ) && isset( $row['status'] ) && in_array( $row['status'], array( 'installed', 'denied', 'failed' ), true ) ) {
				$out[ $id ] = array(
					'status' => $row['status'],
					'note'   => isset( $row['note'] ) && is_string( $row['note'] ) ? substr( preg_replace( '/[\x00-\x1f\x7f]/', ' ', $row['note'] ), 0, 160 ) : '',
				);
			}
		}
		return $out;
	}

	/** waiting, installed, denied, failed or expired (waiting for more than a week: the host has dropped it). */
	function wph_pr_state( $entry, $results ) {
		if ( isset( $results[ $entry['id'] ] ) ) {
			return $results[ $entry['id'] ]['status'];
		}
		return $entry['t'] < time() - 7 * DAY_IN_SECONDS ? 'expired' : 'waiting';
	}

	/** File a request. Returns array( ok, message ). A request for a slug that already waits is a success and adds nothing. */
	function wph_pr_file( $slug, $user ) {
		if ( wph_pr_max() < 1 ) {
			return array( false, 'Requesting plugins is not switched on for this site.' );
		}
		$list    = wph_pr_requests();
		$results = wph_pr_results();
		$waiting = 0;
		foreach ( $list as $entry ) {
			if ( 'waiting' === wph_pr_state( $entry, $results ) ) {
				if ( $entry['slug'] === $slug ) {
					return array( true, 'Already requested.' );
				}
				$waiting++;
			}
		}
		if ( $waiting >= wph_pr_max() ) {
			return array( false, 'There are already ' . $waiting . ' requests waiting. Ask again when the operator has answered some.' );
		}
		$list[] = array(
			'id'   => bin2hex( random_bytes( 6 ) ),
			'slug' => $slug,
			'user' => substr( (string) $user, 0, 60 ),
			't'    => time(),
		);
		update_option( 'wph_plugin_requests', array_slice( $list, -50 ), false );
		return array( true, 'Requested.' );
	}

	/** Administrators get install_plugins back (WordPress takes it away with DISALLOW_FILE_MODS), so the Add New screen exists. Nothing else is given. */
	function wph_pr_caps( $caps, $cap, $user_id ) {
		if ( 'install_plugins' === $cap && wph_pr_max() > 0 && array( 'do_not_allow' ) === $caps && $user_id > 0 && user_can( $user_id, 'activate_plugins' ) ) {
			return array( 'activate_plugins' );
		}
		return $caps;
	}

	/** The Install button's ajax call. WordPress registers its own handler at priority 1, so this runs first and ends the request. */
	function wph_pr_ajax() {
		if ( wph_pr_max() < 1 ) {
			return;
		}
		check_ajax_referer( 'updates' );
		$slug = wph_pr_slug( isset( $_POST['slug'] ) ? wp_unslash( $_POST['slug'] ) : '' );
		if ( '' === $slug ) {
			wp_send_json_error( array( 'slug' => '', 'errorCode' => 'wph_bad_slug', 'errorMessage' => 'That is not a plugin name wordpress.org uses.' ) );
		}
		if ( ! current_user_can( 'install_plugins' ) ) {
			wp_send_json_error( array( 'slug' => $slug, 'errorCode' => 'wph_not_allowed', 'errorMessage' => 'You are not allowed to request plugins.' ) );
		}
		$done = wph_pr_file( $slug, wp_get_current_user()->user_login );
		if ( ! $done[0] ) {
			wp_send_json_error( array( 'slug' => $slug, 'errorCode' => 'wph_refused', 'errorMessage' => $done[1] ) );
		}
		wp_send_json_success( array( 'slug' => $slug, 'pluginName' => $slug, 'wphRequested' => true ) );
	}

	/** The plain-link install (the plugin details window) and the zip upload, both through wp-admin/update.php. */
	function wph_pr_update_php() {
		global $pagenow;
		if ( 'update.php' !== $pagenow || wph_pr_max() < 1 ) {
			return;
		}
		$action = isset( $_REQUEST['action'] ) ? wp_unslash( $_REQUEST['action'] ) : '';
		if ( 'upload-plugin' === $action ) {
			wp_die( 'Uploading a plugin is not available here. Find it on the Add New page and press Install: that sends a request to the hosting operator.', 'Not available', array( 'response' => 403 ) );
		}
		if ( 'install-plugin' !== $action ) {
			return;
		}
		$slug = wph_pr_slug( isset( $_REQUEST['plugin'] ) ? wp_unslash( $_REQUEST['plugin'] ) : '' );
		if ( '' === $slug ) {
			wp_die( 'That is not a plugin name wordpress.org uses.', 'Not available', array( 'response' => 400 ) );
		}
		check_admin_referer( 'install-plugin_' . $slug );
		if ( ! current_user_can( 'install_plugins' ) ) {
			wp_die( 'You are not allowed to request plugins.', 'Not available', array( 'response' => 403 ) );
		}
		$done = wph_pr_file( $slug, wp_get_current_user()->user_login );
		if ( ! $done[0] ) {
			wp_die( esc_html( $done[1] ), 'Not available', array( 'response' => 429 ) );
		}
		wp_safe_redirect( admin_url( 'plugin-install.php' ) );
		exit;
	}

	/** On the Add New screen only: the words say what the button does, the zip upload is hidden. A page that is not that screen is left alone. */
	function wph_pr_words( $translation, $text, $domain = 'default' ) {
		global $pagenow;
		if ( 'plugin-install.php' === $pagenow && 'default' === $domain && 'Install Now' === $text ) {
			return 'Request install';
		}
		return $translation;
	}

	function wph_pr_screen_assets( $hook ) {
		if ( 'plugin-install.php' !== $hook || wph_pr_max() < 1 ) {
			return;
		}
		wp_add_inline_script(
			'updates',
			"if(window.wp&&wp.updates&&wp.updates.l10n){(function(l){l.installNow='Request install';l.installing='Requesting…';l.pluginInstalled='Requested';l.pluginInstalledLabel='Requested %s';" .
			"l.pluginInstallingLabel='Requesting %s';l.pluginInstallNowLabel='Request install of %s';l.installedMsg='Requested. The hosting operator will look at it.';})(wp.updates.l10n);}",
			'after'
		);
	}

	function wph_pr_head() {
		global $pagenow;
		if ( 'plugin-install.php' === $pagenow && wph_pr_max() > 0 ) {
			echo '<style>.upload-view-toggle,.upload-plugin,.wp-upload-form{display:none!important}</style>' . "\n";
		}
	}

	/** Each request and what became of it, on the Plugins pages. */
	function wph_pr_notices() {
		global $pagenow;
		if ( ! in_array( $pagenow, array( 'plugin-install.php', 'plugins.php' ), true ) || wph_pr_max() < 1 || ! current_user_can( 'install_plugins' ) ) {
			return;
		}
		$list = array_slice( array_reverse( wph_pr_requests() ), 0, 10 );
		if ( ! $list ) {
			return;
		}
		$results = wph_pr_results();
		$words   = array(
			'waiting'   => 'waiting for the hosting operator',
			'installed' => 'installed. Activate it on the Plugins page',
			'denied'    => 'declined',
			'failed'    => 'could not be installed',
			'expired'   => 'expired without an answer',
		);
		echo '<div class="notice notice-info"><p><strong>Plugin requests</strong></p><ul>';
		foreach ( $list as $entry ) {
			$state = wph_pr_state( $entry, $results );
			$note  = isset( $results[ $entry['id'] ] ) && '' !== $results[ $entry['id'] ]['note'] ? ' (' . $results[ $entry['id'] ]['note'] . ')' : '';
			echo '<li>' . esc_html( $entry['slug'] . ': ' . $words[ $state ] . $note ) . '</li>';
		}
		echo '</ul></div>';
	}

	add_filter( 'map_meta_cap', 'wph_pr_caps', 99, 3 );
	add_action( 'wp_ajax_install-plugin', 'wph_pr_ajax', 0 );
	add_action( 'admin_init', 'wph_pr_update_php' );
	add_filter( 'gettext', 'wph_pr_words', 10, 3 );
	add_action( 'admin_enqueue_scripts', 'wph_pr_screen_assets' );
	add_action( 'admin_head', 'wph_pr_head' );
	add_action( 'admin_notices', 'wph_pr_notices' );
}
