# DeepSeek queue behavior

Owner-authored peak-time requests remain pending until an off-peak scheduled execution. An owner may withdraw pending requests by their original Issue comment ID. Cancellation cannot revoke an API call already sent. Existing DEEPSEEK_ENABLED=false stops model calls while preserving the pending queue. No automatic retry after an ambiguous API call.