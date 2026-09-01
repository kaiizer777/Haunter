# ssm.tf intentionally left empty.
# The /haunter/GITHUB_TOKEN SSM parameter is managed MANUALLY (aws ssm put-parameter),
# not via terraform. The PEM is sourced from the user's local GITHUB_APP_PRIVATE_KEY
# in backend/.env, which is gitignored. Keeping the PEM out of terraform state avoids
# committing a sensitive value to the .tfstate file.
#
# If you need to re-create the SSM parameter:
#   1. Get the PEM from backend/.env (GITHUB_APP_PRIVATE_KEY value)
#   2. Save to a temp file: temp_pem.txt
#   3. aws ssm put-parameter --name /haunter/GITHUB_TOKEN --value "$(cat temp_pem.txt)" --type SecureString --overwrite
#   4. Delete temp_pem.txt after running
#
# Do NOT add an aws_ssm_parameter resource here — it will work, but the PEM ends up
# in terraform state. Manual management keeps secrets out of state.
