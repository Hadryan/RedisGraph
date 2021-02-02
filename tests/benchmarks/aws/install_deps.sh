#!/bin/bash
apt install -y zip

# Ensure terraform is available
TF_EXE_FILE_NAME=${TF_EXE_FILE_NAME:-$(which terraform)}
if [[ -z "${TF_EXE_FILE_NAME}" ]]; then
    echo "terraform not available. It is not specified explicitly and not found in \$PATH"
    echo "Downloading terraform..."
    wget https://releases.hashicorp.com/terraform/0.13.5/terraform_0.13.5_linux_amd64.zip
    unzip terraform_0.13.5_linux_amd64.zip
    mv terraform /usr/local/sbin
    rm terraform_0.13.5_linux_amd64.zip
    TF_EXE_FILE_NAME=/usr/local/sbin/terraform

fi
echo "Checking terraform version..."
${TF_EXE_FILE_NAME} --version
