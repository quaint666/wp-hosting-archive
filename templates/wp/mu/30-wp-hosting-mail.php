<?php
/**
 * Managed by Ansible (templates/wp/mu/30-wp-hosting-mail.php). A must-use plugin of the hosting service, loaded from WPMU_PLUGIN_DIR (root-owned, read-only to PHP).
 *
 * Mail for a site that cannot send any: the VM and its containers have no outbound SMTP. This answers the pre_wp_mail filter (WordPress 5.7 and later), so wp_mail() never reaches PHPMailer.
 *  - WP_HOSTING_MAIL_MODE 'log' (the default): nothing leaves the site; one line per mail goes to the container's log (recipients and subject, never the body) and wp_mail() reports success.
 *    'send': the mail goes to the provider's HTTPS API (Resend) with a 10 second timeout; wp_mail() reports what the provider said.
 *  - The From address is always WP_HOSTING_MAIL_FROM (a name at the sender subdomain). The address WordPress or a plugin wanted becomes Reply-To, so a reply reaches the right person and the
 *    sender domain stays the one with the DNS records.
 *  - At most WP_HOSTING_MAIL_PER_HOUR and WP_HOSTING_MAIL_PER_DAY mails; past that wp_mail() returns false and says so in the log.
 *  - The provider's key is WP_HOSTING_MAIL_KEY, defined in the rendered wp-config.php (never in the database), and only in send mode.
 * The state (counts, last result, consecutive failures) is kept in the option wph_mail_status for whoever looks. Attachments are not supported and are dropped (the log says so).
 * The Resend request shape is from its public API documentation [from memory]; a log-mode run shows what would be sent, and the first send-mode mail is the proof.
 */
if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

if ( ! function_exists( 'wph_mail_setting' ) ) {
	function wph_mail_setting( $name, $default ) {
		return defined( $name ) ? constant( $name ) : $default;
	}

	/** Addresses out of "a@b, Name <c@d>" or an array of them; whatever is not an address is dropped. */
	function wph_mail_addresses( $value ) {
		$list = is_array( $value ) ? $value : explode( ',', (string) $value );
		$out  = array();
		foreach ( $list as $item ) {
			$item = trim( (string) $item );
			if ( preg_match( '/<([^<>\s]+)>\s*$/', $item, $m ) ) {
				$item = $m[1];
			}
			if ( '' !== $item && false !== is_email( $item ) ) {
				$out[] = $item;
			}
		}
		return array_values( array_unique( $out ) );
	}

	/** The headers WordPress passes as a string or an array, as name => value (a name that repeats keeps the last value). */
	function wph_mail_headers( $headers ) {
		$lines = is_array( $headers ) ? $headers : explode( "\n", str_replace( "\r\n", "\n", (string) $headers ) );
		$out   = array();
		foreach ( $lines as $line ) {
			if ( false === strpos( (string) $line, ':' ) ) {
				continue;
			}
			list( $name, $value ) = explode( ':', (string) $line, 2 );
			$out[ strtolower( trim( $name ) ) ] = trim( $value );
		}
		return $out;
	}

	function wph_mail_status( $change = null ) {
		$status = get_option( 'wph_mail_status', array() );
		$status = is_array( $status ) ? $status : array();
		if ( null === $change ) {
			return $status;
		}
		$status = array_merge( $status, $change );
		update_option( 'wph_mail_status', $status, false );
		return $status;
	}

	/** True when this mail is within the hourly and daily caps; counts it when it is. */
	function wph_mail_allowed() {
		$hour_key = 'wph_mail_h_' . gmdate( 'YmdH' );
		$day_key  = 'wph_mail_d_' . gmdate( 'Ymd' );
		$hour     = (int) get_transient( $hour_key );
		$day      = (int) get_transient( $day_key );
		if ( $hour >= (int) wph_mail_setting( 'WP_HOSTING_MAIL_PER_HOUR', 20 ) || $day >= (int) wph_mail_setting( 'WP_HOSTING_MAIL_PER_DAY', 60 ) ) {
			return false;
		}
		set_transient( $hour_key, $hour + 1, 2 * HOUR_IN_SECONDS );
		set_transient( $day_key, $day + 1, 2 * DAY_IN_SECONDS );
		return true;
	}

	function wph_mail_fail( $atts, $why ) {
		$status = wph_mail_status();
		wph_mail_status( array( 'last_failure' => time(), 'last_failure_reason' => substr( $why, 0, 200 ), 'consecutive_failures' => (int) ( $status['consecutive_failures'] ?? 0 ) + 1 ) );
		error_log( 'wp-hosting: mail failed: ' . substr( preg_replace( '/[^\x20-\x7e]/', '?', $why ), 0, 200 ) );
		do_action( 'wp_mail_failed', new WP_Error( 'wp_mail_failed', $why, $atts ) );
		return false;
	}

	/** The pre_wp_mail filter: null lets WordPress go on, a bool is the answer to wp_mail(). */
	function wph_mail_pre( $pre, $atts ) {
		if ( null !== $pre ) {
			return $pre;
		}
		$atts    = is_array( $atts ) ? $atts : array();
		$mode    = (string) wph_mail_setting( 'WP_HOSTING_MAIL_MODE', 'log' );
		$to      = wph_mail_addresses( $atts['to'] ?? '' );
		$headers = wph_mail_headers( $atts['headers'] ?? '' );
		$subject = (string) ( $atts['subject'] ?? '' );
		$body    = (string) ( $atts['message'] ?? '' );
		if ( ! $to ) {
			return wph_mail_fail( $atts, 'no valid recipient' );
		}
		if ( ! wph_mail_allowed() ) {
			return wph_mail_fail( $atts, 'over the mail limit for this hour or day' );
		}
		if ( ! empty( $atts['attachments'] ) ) {
			error_log( 'wp-hosting: mail attachments are not supported and were dropped' );
		}
		$from = (string) wph_mail_setting( 'WP_HOSTING_MAIL_FROM', '' );
		if ( 'send' === $mode && false === is_email( preg_replace( '/^.*<([^<>]+)>\s*$/', '$1', $from ) ) ) {
			return wph_mail_fail( $atts, 'WP_HOSTING_MAIL_FROM is not set to an address' );
		}
		$reply = wph_mail_addresses( $headers['reply-to'] ?? ( $headers['from'] ?? '' ) );
		$html  = isset( $headers['content-type'] ) && false !== stripos( $headers['content-type'], 'text/html' );
		if ( 'send' !== $mode ) {
			error_log( 'wp-hosting: mail (log mode, not sent) to ' . implode( ',', $to ) . ' subject "' . substr( preg_replace( '/[^\x20-\x7e]/', '?', $subject ), 0, 80 ) . '"' );
			wph_mail_status( array( 'mode' => 'log', 'last_logged' => time() ) );
			return true;
		}
		$payload = array( 'from' => $from, 'to' => $to, 'subject' => $subject, ( $html ? 'html' : 'text' ) => $body );
		if ( $reply ) {
			$payload['reply_to'] = $reply;
		}
		foreach ( array( 'cc', 'bcc' ) as $field ) {
			$extra = wph_mail_addresses( $headers[ $field ] ?? '' );
			if ( $extra ) {
				$payload[ $field ] = $extra;
			}
		}
		$response = wp_remote_post(
			'https://api.resend.com/emails',
			array(
				'timeout' => 10,
				'headers' => array(
					'Authorization' => 'Bearer ' . (string) wph_mail_setting( 'WP_HOSTING_MAIL_KEY', '' ),
					'Content-Type'  => 'application/json',
				),
				'body'    => wp_json_encode( $payload ),
			)
		);
		if ( is_wp_error( $response ) ) {
			return wph_mail_fail( $atts, 'the mail provider could not be reached: ' . $response->get_error_message() );
		}
		$code = (int) wp_remote_retrieve_response_code( $response );
		if ( $code < 200 || $code >= 300 ) {
			return wph_mail_fail( $atts, 'the mail provider answered ' . $code );
		}
		wph_mail_status( array( 'mode' => 'send', 'last_success' => time(), 'consecutive_failures' => 0 ) );
		return true;
	}
}

add_filter( 'pre_wp_mail', 'wph_mail_pre', 10, 2 );
