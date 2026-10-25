<?php
/**
 * Managed by Ansible (templates/wp/mu/10-wp-hosting-login.php). A must-use plugin of the hosting service, loaded from WPMU_PLUGIN_DIR (root-owned, read-only to PHP).
 *
 * Login hardening, the second layer behind nginx's limit_req on /wp-login.php:
 *  - failed logins are counted per visitor address and per user name (a 15 minute window that restarts with every failure); past the limit even a correct password is refused;
 *  - a wrong user name and a wrong password give the same message, so the login form does not say which user names exist;
 *  - XML-RPC, application passwords and the listing of users (?author=N, /wp-json/wp/v2/users, the users sitemap) are off, each unless wp-config allows it.
 * The visitor's address is REMOTE_ADDR and nothing else: nginx has already replaced it with Cloudflare's CF-Connecting-IP, and a header the visitor can set is never believed.
 * Settings come from wp-config.php (templates/wp/site_wp-config.php.j2), never from the database. WP-CLI is the operator and is not throttled.
 */
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}
if ( defined( 'WP_CLI' ) && WP_CLI ) {
	return;
}

if ( ! function_exists( 'wph_login_setting' ) ) {
	function wph_login_setting( $name, $default ) {
		return defined( $name ) ? constant( $name ) : $default;
	}

	function wph_login_ip() {
		$ip = isset( $_SERVER['REMOTE_ADDR'] ) ? (string) $_SERVER['REMOTE_ADDR'] : '';
		return false !== filter_var( $ip, FILTER_VALIDATE_IP ) ? $ip : 'unknown';
	}

	function wph_login_user_key( $username ) {
		return strtolower( trim( (string) $username ) );
	}

	function wph_login_count( $kind, $who ) {
		return (int) get_transient( 'wph_fail_' . $kind . '_' . sha1( $who ) );
	}

	function wph_login_bump( $kind, $who ) {
		set_transient( 'wph_fail_' . $kind . '_' . sha1( $who ), wph_login_count( $kind, $who ) + 1, (int) wph_login_setting( 'WP_HOSTING_LOGIN_WINDOW_S', 900 ) );
	}

	function wph_login_locked( $username ) {
		$by_ip = wph_login_count( 'ip', wph_login_ip() ) >= (int) wph_login_setting( 'WP_HOSTING_LOGIN_LIMIT_IP', 20 );
		$key   = wph_login_user_key( $username );
		$by_us = '' !== $key && wph_login_count( 'user', $key ) >= (int) wph_login_setting( 'WP_HOSTING_LOGIN_LIMIT_USER', 10 );
		return $by_ip || $by_us;
	}

	function wph_login_failed( $username ) {
		wph_login_bump( 'ip', wph_login_ip() );
		$key = wph_login_user_key( $username );
		if ( '' !== $key ) {
			wph_login_bump( 'user', $key );
		}
		// One line per failure in the container's log, for whoever reads it. The name is cut and stripped of control characters: it is what the visitor typed.
		error_log( 'wp-hosting: failed login for "' . substr( preg_replace( '/[^\x20-\x7e]/', '?', (string) $username ), 0, 40 ) . '" from ' . wph_login_ip() );
	}

	/**
	 * Runs last on the authenticate filter: a locked visitor gets an error whatever the password was, and the several ways of being wrong become one message.
	 */
	function wph_login_authenticate( $user, $username = '' ) {
		if ( '' === (string) $username && is_wp_error( $user ) ) {
			return $user;      // the form was opened, nothing was tried
		}
		if ( wph_login_locked( $username ) ) {
			return new WP_Error( 'wph_locked', 'Too many failed logins. Try again in a while.' );
		}
		if ( is_wp_error( $user ) ) {
			$codes = $user->get_error_codes();
			if ( array_intersect( $codes, array( 'invalid_username', 'invalid_email', 'incorrect_password' ) ) ) {
				return new WP_Error( 'wph_bad_login', 'The user name or the password is not correct.' );
			}
		}
		return $user;
	}

	function wph_login_hide_users_endpoints( $endpoints ) {
		if ( ! is_user_logged_in() ) {
			unset( $endpoints['/wp/v2/users'], $endpoints['/wp/v2/users/(?P<id>[\d]+)'] );
		}
		return $endpoints;
	}

	function wph_login_hide_author_query() {
		if ( isset( $_GET['author'] ) && ! is_user_logged_in() ) {
			status_header( 404 );
			nocache_headers();
			exit;
		}
	}

	function wph_login_no_users_sitemap( $provider, $name ) {
		return 'users' === $name ? false : $provider;
	}

	function wph_login_allow( $name ) {
		return (bool) wph_login_setting( $name, false );
	}
}

add_action( 'wp_login_failed', 'wph_login_failed', 10, 1 );
add_filter( 'authenticate', 'wph_login_authenticate', 99, 2 );

if ( ! wph_login_allow( 'WP_HOSTING_ALLOW_XMLRPC' ) ) {
	add_filter( 'xmlrpc_enabled', '__return_false' );
}
if ( ! wph_login_allow( 'WP_HOSTING_ALLOW_APP_PASSWORDS' ) ) {
	add_filter( 'wp_is_application_passwords_available', '__return_false' );
}
if ( ! wph_login_allow( 'WP_HOSTING_ALLOW_USER_ENUM' ) ) {
	add_filter( 'rest_endpoints', 'wph_login_hide_users_endpoints' );
	add_action( 'init', 'wph_login_hide_author_query', 1 );
	add_filter( 'wp_sitemaps_add_provider', 'wph_login_no_users_sitemap', 10, 2 );
}
