# Spark download attribution gap

A website download click cannot be tied to the install it produces: `/download/spark` is a Vercel redirect to a fixed GitHub asset, and neither the DMG nor the NSIS installer can carry an id. There is no `dl_id`. Attribution uses the referral (`spark_channel` / `spark_referrer`, set once on the referred person) and the website's first-touch source instead.
