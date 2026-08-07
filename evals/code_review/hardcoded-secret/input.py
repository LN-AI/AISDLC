import boto3

AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
client = boto3.client("s3", aws_secret_access_key=AWS_SECRET)
