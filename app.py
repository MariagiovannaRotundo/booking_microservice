import decimal
import json
import os
from datetime import datetime, timedelta

import connexion
from flask import request, current_app
from sqlalchemy import extract

from database import init_db, Reservation, Friend
import logging

from model.opening_hours_model import OpeningHoursModel
from services.user_service import UserService
from services.booking_service import BookingService
from services.restaurant_service import RestaurantService
from services.send_email_service import SendEmailService
from utils.http_utils import HttpUtils

db_session = None


def _get_response(message: str, code: int, is_custom_obj: bool = False):
    """
    This method contains the code to make a new response for flask view
    :param message: Message response
    :param code: Code result
    :return: a json object that look like {"result": "OK"}
    """
    if is_custom_obj is False:
        return {"result": message}, code
    return message, code


def create_booking(private=False):
    if private is False:
        json = request.get_json()
    else:
        json = private
    current_app.logger.debug("Request received: {}".format(json))
    restaurant_id = json["restaurant_id"]
    user_id = json["user_id"]
    raw_friends = json["raw_friends"]
    py_datetime = datetime.strptime(json["datetime"], "%Y-%m-%dT%H:%M:%SZ")
    people_number = json["people_number"]
    current_app.logger.debug("Translated obj to vars")
    # split friends mail and check if the number is correct
    if people_number > 1:
        splitted_friends = raw_friends.split(";")
        if len(splitted_friends) != (people_number - 1):
            return HttpUtils.error_message(
                400, "You need to specify ONE mail for each person"
            )
        current_app.logger.debug("Friends: {}".format(str(splitted_friends)))
    # if user wants to book in the past..
    if py_datetime < datetime.now() and "is_debug" not in json:
        return HttpUtils.error_message(400, "You can not book in the past!")

    # check if the user is positive
    current_user = UserService.get_user_info(user_id)
    current_app.logger.debug("user is positive ? {}".format(current_user.is_positive))
    if current_user.is_positive:
        return HttpUtils.error_message(401, "You are marked as positive!"), 401
    week_day = py_datetime.weekday()

    # check if the restaurant is open. 12 in open_lunch means open at lunch. 20 in open_dinner means open at dinner.
    openings = RestaurantService.get_openings(restaurant_id)
    current_app.logger.debug("Got {} openings".format(len(openings)))
    # the restaurant is closed
    if openings is None or len(openings) == 0:
        current_app.logger.debug("No open hours")
        return HttpUtils.error_message(404, "The restaurant is closed")

    opening_hour_json = BookingService.filter_openings(openings, week_day=week_day)[0]
    current_app.logger.debug("Got openings this day: {}".format(opening_hour_json))
    # the restaurant is closed
    if opening_hour_json is None:  # TODO: Test
        print("No Opening hour")
        return HttpUtils.error_message(404, "The restaurant is closed")

    # bind to obj
    opening_hour = OpeningHoursModel()
    opening_hour.fill_from_json(opening_hour_json)
    current_app.logger.debug("Binded, weekday: {}".format(str(opening_hour.week_day)))

    # check if restaurant is open
    response = BookingService.check_restaurant_openings(opening_hour, py_datetime)
    current_app.logger.debug("Restaurant checked, i got: {}".format(str(response)))
    if response is not True:
        return response
    # now let's see if there is a table
    """
    get the time delta (avg_time) e name from the restaurant
    """
    restaurant_info = RestaurantService.get_info(restaurant_id)
    restaurant_name = restaurant_info["name"]
    avg_time = restaurant_info["avg_time"]

    """
    get all the reservation (with the reservation_date between the dates in which I want to book)
    or (or the reservation_end between the dates in which I want to book)
    the dates in which I want to book are:
    start = py_datetime  
    end = py_datetime + avg_time
    always filtered by the people_number  
    """

    # from the list of all tables in the restaurant (the ones in which max_seats < number of people requested)
    # drop the reserved ones
    all_table_list = RestaurantService.get_tables(restaurant_id)
    if all_table_list is None:
        return HttpUtils.error_message(500, "Can't retrieve restaurant tables")

    free_tables = BookingService.get_free_tables(
        all_table_list, people_number, py_datetime, avg_time
    )

    # if there are tables available.. get the one with minimum max_seats
    current_app.logger.debug(
        "OK, There are {} tables available".format(len(free_tables))
    )
    if len(free_tables) > 0:
        chosen_table = BookingService.get_min_seats_table(free_tables)
        current_app.logger.debug("OK, table {} has been chosen".format(chosen_table))
        # get table name
        table_name = BookingService.get_table_name(all_table_list, chosen_table)
        current_app.logger.debug("His name is: {}".format(table_name))
        # register on db the reservation
        new_reservation = Reservation()
        new_reservation.reservation_date = py_datetime
        new_reservation.reservation_end = py_datetime + timedelta(minutes=avg_time)
        new_reservation.customer_id = user_id
        new_reservation.table_id = chosen_table
        new_reservation.people_number = people_number
        db_session.add(new_reservation)
        db_session.flush()
        current_app.logger.debug("Reservation saved.")
        if people_number > 1:
            # register friends
            for friend_mail in splitted_friends:
                new_friend = Friend()
                new_friend.reservation_id = new_reservation.id
                new_friend.email = friend_mail.strip()
                db_session.add(new_friend)
        else:
            splitted_friends = []
        db_session.commit()

        SendEmailService.booking_confirmation(
            current_user.email,
            current_user.firstname,
            restaurant_name,
            splitted_friends,
            new_reservation.reservation_date,
        )

        return {
            "id": new_reservation.id,
            "restaurant_name": restaurant_name,
            "table_name": table_name,
        }, 200
    else:
        return HttpUtils.error_message(404, "No tables available")


def delete_booking(reservation_id, user_id):
    query = (
        db_session.query(Reservation)
        .filter_by(id=reservation_id)
        .filter_by(customer_id=user_id)
    )

    to_delete = query.first()
    if to_delete is None:
        return HttpUtils.error_message(404, "Reservation not Found")

    query.delete()
    db_session.commit()
    return {"code": 200, "message": "Deleted Successfully"}, 200


def get_booking(reservation_id):
    reservation = db_session.query(Reservation).filter_by(id=reservation_id).first()

    if reservation is None:
        return HttpUtils.error_message(404, "Reservation not Found")

    return BookingService.reservation_to_json(reservation), 200


def get_all_bookings(user_id=False, fromDate=False, toDate=False, restaurant_id=False):

    reservations = db_session.query(Reservation)
    # Filtering stuff
    if user_id is not False:
        current_app.logger.debug(
            "Adding reservation with filter by user id: {}".format(user_id)
        )
        reservations = reservations.filter(Reservation.customer_id == user_id)
    if fromDate is not False:
        current_app.logger.debug(
            "Adding reservation with filter from date: {}".format(fromDate)
        )
        reservations = reservations.filter(
            Reservation.reservation_date
            >= datetime.strptime(fromDate, "%Y-%m-%dT%H:%M:%SZ")
        )
    if toDate is not False:
        current_app.logger.debug(
            "Adding reservation with filter to date: {}".format(toDate)
        )
        reservations = reservations.filter(
            Reservation.reservation_end
            <= datetime.strptime(toDate, "%Y-%m-%dT%H:%M:%SZ")
        )
    if restaurant_id is not False:
        current_app.logger.debug(
            "Adding reservation with filter by restaurant id: {}".format(restaurant_id)
        )
        tables = RestaurantService.get_tables(restaurant_id)
        ints = [table["id"] for table in tables]
        current_app.logger.debug("TABLES INTS: {}".format(ints))
        reservations = reservations.filter(Reservation.table_id.in_(ints))

    reservations = reservations.all()
    if reservations is None:
        return HttpUtils.error_message(404, "No Reservations")

    current_app.logger.debug("reservations len={}".format(len(reservations)))
    for i, reservation in enumerate(reservations):
        reservations[i] = BookingService.replace_with_restaurant(reservation)
        current_app.logger.debug("adding people")
        listfriends = []
        current_app.logger.debug("added empty list")
        friends = (
            db_session.query(Friend).filter_by(reservation_id=reservation.id).all()
        )
        current_app.logger.debug("Got friends: {}".format(len(friends)))
        for friend in friends:
            listfriends.append(friend.email.strip())
        current_app.logger.debug("Frinds: {}".format(listfriends))
        reservations[i].people = listfriends
    return BookingService.reservations_to_json(reservations), 200


def get_all_bookings_restaurant(fromDate=False, toDate=False, restaurant_id=False):

    reservations = db_session.query(Reservation)
    tables = RestaurantService.get_tables(restaurant_id)
    ints = [table["id"] for table in tables]
    current_app.logger.debug("TABLES INTS: {}".format(ints))
    reservations = reservations.filter(Reservation.table_id.in_(ints))

    # Filtering stuff
    if fromDate is not False:
        reservations = reservations.filter(
            Reservation.reservation_date
            >= datetime.strptime(fromDate, "%Y-%m-%dT%H:%M:%SZ")
        )
    if toDate is not False:
        reservations = reservations.filter(
            Reservation.reservation_end
            <= datetime.strptime(toDate, "%Y-%m-%dT%H:%M:%SZ")
        )

    reservations = reservations.all()
    if reservations is None:
        return HttpUtils.error_message(404, "No Reservations")

    current_app.logger.debug("reservations len={}".format(len(reservations)))
    for i, reservation in enumerate(reservations):
        reservations[i] = BookingService.replace_with_customer(reservation)

    return BookingService.reservations_to_json(reservations, "customer"), 200


def update_booking(reservation_id):
    json = request.get_json()

    response, code = delete_booking(reservation_id, json["user_id"])
    if code != 200:
        return code

    response, code = create_booking(json)
    if code != 200:
        return HttpUtils.error_message(
            500, "Internal Error, the booking has been deleted, please replace it"
        )
    return response, code


def check_in(reservation_id):
    reservation = db_session.query(Reservation).filter_by(id=reservation_id).first()
    if reservation is not None:
        reservation.checkin = True
        db_session.commit()
        db_session.flush()
        return {"code": 200, "message": "Success"}, 200
    return HttpUtils.error_message(404, "Reservation not found")


def people_in(restaurant_id):
    openings = RestaurantService.get_openings(restaurant_id)
    if openings is None or len(openings) == 0:
        return {"lunch": 0, "dinner": 0, "now": 0}

    openings = BookingService.filter_openings(openings, datetime.today().weekday())[0]

    openings_model = OpeningHoursModel()
    openings_model.fill_from_json(openings)

    tables = RestaurantService.get_tables(restaurant_id)
    if tables is None or len(tables) == 0:
        return {"lunch": 0, "dinner": 0, "now": 0}

    tables_id = [table["id"] for table in tables]
    current_app.logger.debug("TABLES IDs: {}".format(tables_id))

    reservations_l = (
        db_session.query(Reservation)
        .filter(
            Reservation.table_id.in_(tables_id),
            extract("day", Reservation.reservation_date)
            == extract("day", datetime.today()),
            extract("month", Reservation.reservation_date)
            == extract("month", datetime.today()),
            extract("year", Reservation.reservation_date)
            == extract("year", datetime.today()),
            extract("hour", Reservation.reservation_date)
            >= extract("hour", openings_model.open_lunch),
            extract("hour", Reservation.reservation_date)
            <= extract("hour", openings_model.close_lunch),
        )
        .all()
    )

    reservations_d = (
        db_session.query(Reservation)
        .filter(
            Reservation.table_id.in_(tables_id),
            extract("day", Reservation.reservation_date)
            == extract("day", datetime.today()),
            extract("month", Reservation.reservation_date)
            == extract("month", datetime.today()),
            extract("year", Reservation.reservation_date)
            == extract("year", datetime.today()),
            extract("hour", Reservation.reservation_date)
            >= extract("hour", openings_model.open_dinner),
            extract("hour", Reservation.reservation_date)
            <= extract("hour", openings_model.close_dinner),
        )
        .all()
    )

    reservations_now = (
        db_session.query(Reservation)
        .filter(
            Reservation.checkin is True,
            Reservation.reservation_date <= datetime.now(),
            Reservation.reservation_end >= datetime.now(),
        )
        .all()
    )

    current_app.logger.debug("End of queries")
    return {
        "lunch": len(reservations_l),
        "dinner": len(reservations_d),
        "now": len(reservations_now),
    }, 200


# --------- END API definition --------------------------
logging.basicConfig(level=logging.DEBUG)
app = connexion.App(__name__)

application = app.app
if "GOUOUTSAFE_TEST" in os.environ and os.environ["GOUOUTSAFE_TEST"] == "1":
    db_session = init_db("sqlite:///tests/booking.db")
else:
    db_session = init_db("sqlite:///booking.db")
app.add_api("swagger.yml")


def _init_flask_app(flask_app, conf_type: str = "config.DebugConfiguration"):
    """
    This method init the flask app
    :param flask_app:
    """
    flask_app.config.from_object(conf_type)
    flask_app.config["DB_SESSION"] = db_session


@application.teardown_appcontext
def shutdown_session(exception=None):
    if db_session is not None:
        db_session.remove()


if __name__ == "__main__":
    #_init_flask_app(application)
    _init_flask_app(application, "config.BaseConfiguration")
    app.run(port=5004)
