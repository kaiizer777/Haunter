# ssm.tf intentionally left empty.
# The /haunter/GITHUB_APP_PRIVATE_KEY (PR-write App) and
# /haunter/GITHUB_SANDBOX_APP_PRIVATE_KEY (sandbox runner App) SSM parameters
# are managed MANUALLY (aws ssm put-parameter), not via terraform. The PEM is
# sourced from the user's local GITHUB_APP_PRIVATE_KEY in backend/.env, which
# is gitignored. Keeping the PEM out of terraform state avoids committing a
# sensitive value to the .tfstate file.
#
# If you need to (re-)create the PR-write App SSM parameter:
#   1. Get the PEM from backend/.env (GITHUB_APP_PRIVATE_KEY value)
#   2. Save to a temp file: temp_pem.txt
#   3. aws ssm put-parameter --name /haunter/GITHUB_APP_PRIVATE_KEY --value "$(cat temp_pem.txt)" --type SecureString --overwrite
#   4. Delete temp_pem.txt after running
# (Same steps with --name /haunter/GITHUB_SANDBOX_APP_PRIVATE_KEY for the
# sandbox runner App PEM.)
#
# Do NOT add an aws_ssm_parameter resource here — it will work, but the PEM ends up
# in terraform state. Manual management keeps secrets out of state.
