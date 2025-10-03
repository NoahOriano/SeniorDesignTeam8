import smtplib
import ssl
import os

class EmailHandler:
    def __init__(self):
        self.smtp_server = "smtp.gmail.com"
        self.port = 465

    def send_email(self, sender_email, sender_password, receiver_email, subject, body):
        """Sends an email using Gmail's SMTP server.

        Args:
            sender_email (str): The sender's Gmail address.
            sender_password (str): The sender's Gmail app password.
            receiver_email (str): The recipient's email address.
            subject (str): The subject of the email.
            body (str): The body content of the email.

        Returns:
            bool: True if the email was sent successfully, False otherwise.
        """
        message = f"Subject: {subject}\n\n{body}"

        context = ssl.create_default_context()

        try:
            with smtplib.SMTP_SSL(self.smtp_server, self.port, context=context) as server:
                server.login(sender_email, sender_password)
                server.sendmail(sender_email, receiver_email, message.encode('utf-8'))
            print(f"Email sent successfully to {receiver_email}")
            return True
        except smtplib.SMTPAuthenticationError:
            print("SMTP Authentication Error: Check your email/password or app password.")
            return False
        except Exception as e:
            print(f"Failed to send email: {e}")
            return False
