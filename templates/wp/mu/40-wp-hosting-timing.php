<?php
/**
 * Managed by Ansible (templates/wp/mu/40-wp-hosting-timing.php). A must-use plugin of the hosting service, loaded from WPMU_PLUGIN_DIR (root-owned, read-only to PHP).
 *
 * Where a request's time goes, measured inside php-fpm where it happens. OFF unless WP_HOSTING_TIMING (wp_php_timing in host_vars/dellwp/vars.yml) is 1 or 2: off, this file defines its functions
 * and registers nothing. On, every request that reaches PHP (a page the nginx cache answers never does) writes ONE line to the container's log, "wph-timing {json}", which
 * scripts/wp_perf_measure.sh summarises (docker logs <the php container>).
 *   1  when each stage of WordPress's start-up was reached, in milliseconds since the request reached PHP (mu-plugins, plugins, theme setup, init, wp_loaded, the query, the template, shutdown);
 *      how long each plugin's file took to include; the number of queries; peak memory; and opcache's own gauges: memory used, free and wasted, scripts cached against the limit, hit rate, and the
 *      restarts opcache was forced into because it was full. Those gauges answer "is the file limit too low" from the running pool instead of from a guess.
 *   2  all of that, and every callback hooked to those stages is timed and charged to the plugin, theme or core file it was written in, so "init took 80 ms" becomes "elementor 31, woocommerce 27".
 *      The time is inclusive (a callback that fires other hooks includes their time) and the wrapping costs a few percent: use it for looking, then set it back to 0.
 * Nothing in the line is a secret: the method, the path without its query string, the values of wc-ajax, rest_route and action (letters, digits and . _ / - only), the status and numbers.
 * It never changes what a request does: a failure inside this file is swallowed.
 */
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

if ( ! function_exists( 'wph_timing_level' ) ) {
	function wph_timing_level() {
		return defined( 'WP_HOSTING_TIMING' ) ? (int) constant( 'WP_HOSTING_TIMING' ) : 0;
	}

	/** Milliseconds since the request reached PHP (REQUEST_TIME_FLOAT is set by the SAPI before any PHP of ours runs). */
	function wph_timing_ms() {
		$start = isset( $_SERVER['REQUEST_TIME_FLOAT'] ) ? (float) $_SERVER['REQUEST_TIME_FLOAT'] : (float) $GLOBALS['wph_timing']['t0'];
		return round( ( microtime( true ) - $start ) * 1000, 1 );
	}

	/** Whose code a file is: a plugin, a theme, our must-use plugins, or core. */
	function wph_timing_owner_of_file( $file ) {
		$file = (string) $file;
		if ( preg_match( '#/wp-content/plugins/([^/]+)#', $file, $m ) ) {
			return 'plugin ' . $m[1];
		}
		if ( preg_match( '#/wp-content/themes/([^/]+)#', $file, $m ) ) {
			return 'theme ' . $m[1];
		}
		if ( defined( 'WPMU_PLUGIN_DIR' ) && 0 === strpos( $file, (string) constant( 'WPMU_PLUGIN_DIR' ) ) ) {
			return 'mu-plugins (ours)';
		}
		if ( false !== strpos( $file, '/wp-includes/' ) ) {
			return 'wp-includes';
		}
		if ( false !== strpos( $file, '/wp-admin/' ) ) {
			return 'wp-admin';
		}
		return 'other';
	}

	/** Whose code a callback is, from the file its function or method was written in. */
	function wph_timing_owner_of_callback( $fn ) {
		try {
			if ( is_array( $fn ) && isset( $fn[0], $fn[1] ) ) {
				$ref = new ReflectionMethod( $fn[0], $fn[1] );
			} elseif ( is_object( $fn ) && ! ( $fn instanceof Closure ) ) {
				$ref = new ReflectionMethod( $fn, '__invoke' );
			} elseif ( is_string( $fn ) && false !== strpos( $fn, '::' ) ) {
				$ref = new ReflectionMethod( $fn );
			} else {
				$ref = new ReflectionFunction( $fn );
			}
			$file = $ref->getFileName();
			return $file ? wph_timing_owner_of_file( $file ) : 'php internal';
		} catch ( Throwable $e ) {
			return 'unknown';
		}
	}

	/** A callback that adds the time it took to its owner's total; what it returns, and what it was given, are untouched. */
	function wph_timing_wrapper( $fn ) {
		$owner = wph_timing_owner_of_callback( $fn );
		return function ( ...$args ) use ( $fn, $owner ) {
			$t0 = microtime( true );
			try {
				return $fn( ...$args );
			} finally {
				$GLOBALS['wph_timing']['owners'][ $owner ] = ( isset( $GLOBALS['wph_timing']['owners'][ $owner ] ) ? $GLOBALS['wph_timing']['owners'][ $owner ] : 0 ) + ( microtime( true ) - $t0 ) * 1000;
			}
		};
	}

	/** Level 2: wrap what is hooked to $hook at normal priorities. Done when the hook starts, from a callback at priority -1000, before the others run. */
	function wph_timing_wrap_hook( $hook ) {
		global $wp_filter;
		if ( empty( $wp_filter[ $hook ] ) || ! is_object( $wp_filter[ $hook ] ) || empty( $wp_filter[ $hook ]->callbacks ) ) {
			return;
		}
		foreach ( $wp_filter[ $hook ]->callbacks as $priority => $items ) {
			if ( $priority < -900 ) {
				continue;
			}
			foreach ( $items as $id => $item ) {
				if ( isset( $item['wph_wrapped'] ) || empty( $item['function'] ) ) {
					continue;
				}
				$wp_filter[ $hook ]->callbacks[ $priority ][ $id ]['function']     = wph_timing_wrapper( $item['function'] );
				$wp_filter[ $hook ]->callbacks[ $priority ][ $id ]['wph_wrapped'] = true;
			}
		}
	}

	/** A stage of the start-up was reached (the first time only): note when, and at level 2 wrap what is hooked to it. */
	function wph_timing_on_hook() {
		try {
			$name = current_filter();
			if ( ! isset( $GLOBALS['wph_timing']['marks'][ $name ] ) ) {
				$GLOBALS['wph_timing']['marks'][ $name ] = wph_timing_ms();
			}
			$GLOBALS['wph_timing']['last'] = microtime( true );
			if ( wph_timing_level() >= 2 ) {
				wph_timing_wrap_hook( $name );
			}
		} catch ( Throwable $e ) {
			return;
		}
	}

	/** A plugin file has just been included: what passed since the last event is the cost of including it. */
	function wph_timing_on_plugin_loaded( $file = '' ) {
		try {
			$now   = microtime( true );
			$owner = wph_timing_owner_of_file( $file );
			$prev  = isset( $GLOBALS['wph_timing']['plugins'][ $owner ] ) ? $GLOBALS['wph_timing']['plugins'][ $owner ] : 0;
			$GLOBALS['wph_timing']['plugins'][ $owner ] = $prev + ( $now - ( isset( $GLOBALS['wph_timing']['last'] ) ? $GLOBALS['wph_timing']['last'] : $now ) ) * 1000;
			$GLOBALS['wph_timing']['last']              = $now;
		} catch ( Throwable $e ) {
			return;
		}
	}

	function wph_timing_mb( $bytes ) {
		return round( (float) $bytes / 1048576, 1 );
	}

	/** The largest $n entries of name => ms, rounded. */
	function wph_timing_top( $list, $n ) {
		arsort( $list );
		$out = array();
		foreach ( array_slice( $list, 0, $n, true ) as $name => $ms ) {
			$out[ $name ] = round( $ms, 1 );
		}
		return $out;
	}

	/** opcache's own gauges, or null when it is not there. */
	function wph_timing_opcache() {
		if ( ! function_exists( 'opcache_get_status' ) ) {
			return null;
		}
		$s = opcache_get_status( false );
		if ( ! is_array( $s ) || empty( $s['opcache_enabled'] ) ) {
			return array( 'on' => 0 );
		}
		$m = isset( $s['memory_usage'] ) ? $s['memory_usage'] : array();
		$i = isset( $s['interned_strings_usage'] ) ? $s['interned_strings_usage'] : array();
		$o = isset( $s['opcache_statistics'] ) ? $s['opcache_statistics'] : array();
		return array(
			'on'            => 1,
			'full'          => empty( $s['cache_full'] ) ? 0 : 1,
			'used_mb'       => wph_timing_mb( isset( $m['used_memory'] ) ? $m['used_memory'] : 0 ),
			'free_mb'       => wph_timing_mb( isset( $m['free_memory'] ) ? $m['free_memory'] : 0 ),
			'wasted_pct'    => round( isset( $m['current_wasted_percentage'] ) ? (float) $m['current_wasted_percentage'] : 0, 1 ),
			'scripts'       => isset( $o['num_cached_scripts'] ) ? (int) $o['num_cached_scripts'] : 0,
			'keys'          => isset( $o['num_cached_keys'] ) ? (int) $o['num_cached_keys'] : 0,
			'max_keys'      => isset( $o['max_cached_keys'] ) ? (int) $o['max_cached_keys'] : 0,
			'hit_pct'       => round( isset( $o['opcache_hit_rate'] ) ? (float) $o['opcache_hit_rate'] : 0, 1 ),
			'oom_restarts'  => isset( $o['oom_restarts'] ) ? (int) $o['oom_restarts'] : 0,
			'hash_restarts' => isset( $o['hash_restarts'] ) ? (int) $o['hash_restarts'] : 0,
			'str_used_mb'   => wph_timing_mb( isset( $i['used_memory'] ) ? $i['used_memory'] : 0 ),
			'str_free_mb'   => wph_timing_mb( isset( $i['free_memory'] ) ? $i['free_memory'] : 0 ),
		);
	}

	/** The one line, at the very end of the request (a shutdown function runs even after an exit, which the shutdown action does not). */
	function wph_timing_log() {
		try {
			if ( empty( $GLOBALS['wph_timing'] ) ) {
				return;
			}
			$t   = $GLOBALS['wph_timing'];
			$uri = isset( $_SERVER['REQUEST_URI'] ) ? (string) $_SERVER['REQUEST_URI'] : '/';
			$row = array(
				'm'     => isset( $_SERVER['REQUEST_METHOD'] ) ? (string) $_SERVER['REQUEST_METHOD'] : '',
				'p'     => substr( (string) parse_url( $uri, PHP_URL_PATH ), 0, 120 ),
				'st'    => http_response_code(),
				'ms'    => wph_timing_ms(),
				'level' => wph_timing_level(),
			);
			foreach ( array( 'wc-ajax', 'rest_route', 'action' ) as $key ) {
				if ( isset( $_REQUEST[ $key ] ) && is_string( $_REQUEST[ $key ] ) ) {
					$row['q'][ $key ] = substr( (string) preg_replace( '/[^A-Za-z0-9_\/.-]/', '', $_REQUEST[ $key ] ), 0, 40 );
				}
			}
			$row['marks'] = $t['marks'];
			$row['inc']   = wph_timing_top( $t['plugins'], 10 );
			if ( ! empty( $t['owners'] ) ) {
				$row['own'] = wph_timing_top( $t['owners'], 12 );
			}
			$row['queries'] = function_exists( 'get_num_queries' ) ? get_num_queries() : null;
			$row['mem_mb']  = wph_timing_mb( memory_get_peak_usage( true ) );
			$row['oc']      = wph_timing_opcache();
			error_log( 'wph-timing ' . wp_json_encode( $row ) );
		} catch ( Throwable $e ) {
			return;
		}
	}

	/** Switch it on: the stages to note, the plugin loads to time, and the line at the end. */
	function wph_timing_boot() {
		$GLOBALS['wph_timing'] = array( 't0' => microtime( true ), 'marks' => array(), 'plugins' => array(), 'owners' => array(), 'last' => microtime( true ) );
		$stages = array( 'muplugins_loaded', 'plugins_loaded', 'after_setup_theme', 'init', 'wp_loaded', 'parse_request', 'wp', 'template_redirect', 'rest_api_init', 'admin_init',
			'wp_enqueue_scripts', 'wp_head', 'wp_footer', 'woocommerce_init', 'elementor/init', 'shutdown' );
		foreach ( $stages as $stage ) {
			add_action( $stage, 'wph_timing_on_hook', -1000, 0 );
		}
		add_action( 'plugin_loaded', 'wph_timing_on_plugin_loaded', -1000, 1 );
		register_shutdown_function( 'wph_timing_log' );
	}
}

if ( wph_timing_level() > 0 ) {
	wph_timing_boot();
}
