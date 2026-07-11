UPDATE stage_attempts
SET retryable = 1, error_code = 'evaluation_transport_failed'
WHERE state = 'failed'
  AND stage_name = 'evaluation'
  AND error_code = 'evaluation_precondition_failed'
  AND (
      error_summary LIKE '%10053%'
      OR error_summary LIKE '%10054%'
      OR error_summary LIKE '%10060%'
  );
