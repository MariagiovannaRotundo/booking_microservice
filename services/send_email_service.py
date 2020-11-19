import requests
from flask import current_app
from app_constant import EMAIL_MICROSERVICE_URL


class SendEmailService:
    """
    This method contains all the logic to
    send the email with send email microservices
    """

    @staticmethod
    def booking_confirmation(email: str, restaurant_name: str, splitted_friends, reservation_date):
        """
        :param email: Email of the new user
        :param name: Name of the new user
        """
        current_app.logger.debug("Email to send the email: {}".format(email))
        json = {
            "email": email,
            "restaurant_name": restaurant_name,
            "splitted_friends": splitted_friends,
            "reservation_date": reservation_date
        }
        current_app.logger.debug("JSON request {}".format(json))
        url = "{}/booking_confirmation".format(EMAIL_MICROSERVICE_URL)
        current_app.logger.debug("URL to microservices sendemail {}".format(url))
        response = requests.post(url=url, json=json)
        json = response.json()
        if response.ok is False:
            current_app.logger.error("Error during the request: {}".format(response.status_code))
            current_app.logger.error("Error with message {}".format(json))
            return False
        return True
