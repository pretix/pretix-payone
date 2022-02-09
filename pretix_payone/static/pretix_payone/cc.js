$(function () {

    Payone.ClientApi.Language[$("#payone_lng").text()].placeholders.cardpan = '4444444444444444'
    Payone.ClientApi.Language[$("#payone_lng").text()].placeholders.cvc = '123'
    Payone.ClientApi.Language[$("#payone_lng").text()].placeholders.expireMonth = '12'
    Payone.ClientApi.Language[$("#payone_lng").text()].placeholders.expireYear = '2099'


    var request,
        supportedCardtypes = ["#"].concat(JSON.parse($.trim($("#payone_cardtypes").html()))),
        config;

    config = {
        fields: {
            cardpan: {
                selector: "payone_cardpan",
                type: "input"
            },
            cardcvc2: {
                selector: "payone_cardcvc2",
                type: "password",
                maxlength: "4",
                length: {"a": 4, "V": 3, "J": false}
            },
            cardexpiremonth: {
                selector: "payone_cardexpiremonth",
                type: "text",
                maxlength: "2",
            },
            cardexpireyear: {
                selector: "payone_cardexpireyear",
                type: "text",
            },
            cardtype: {
                selector: "payone_cardtype",
                cardtypes: supportedCardtypes
            }
        },
        defaultStyle: {
            input: "display: block;\n" +
                "    width: 100%;\n" +
                "    height: 34px;\n" +
                "    padding: 6px 12px;\n" +
                "    font-size: 14px;\n" +
                "    line-height: 1.42857;\n" +
                "    color: #555;\n" +
                "    background-color: #fff;\n" +
                "    background-image: none;\n" +
                "    border: 1px solid #ccc;\n" +
                "    border-radius: 3px;",
            inputFocus: "display: block;\n" +
                "    width: 100%;\n" +
                "    height: 34px;\n" +
                "    padding: 6px 12px;\n" +
                "    font-size: 14px;\n" +
                "    line-height: 1.42857;\n" +
                "    color: #555;\n" +
                "    background-color: #fff;\n" +
                "    background-image: none;\n" +
                "    border: 1px solid #66afe9;\n" +
                "    border-radius: 3px;" +
                "    outline: 0;",
            select: "display: block;\n" +
                "    width: 100%;\n" +
                "    height: 34px;\n" +
                "    padding: 6px 12px;\n" +
                "    font-size: 14px;\n" +
                "    line-height: 1.42857;\n" +
                "    color: #555;\n" +
                "    background-color: #fff;\n" +
                "    background-image: none;\n" +
                "    border: 1px solid #ccc;\n" +
                "    border-radius: 3px;",
            selectFocus: "display: block;\n" +
                "    width: 100%;\n" +
                "    height: 34px;\n" +
                "    padding: 6px 12px;\n" +
                "    font-size: 14px;\n" +
                "    line-height: 1.42857;\n" +
                "    color: #555;\n" +
                "    background-color: #fff;\n" +
                "    background-image: none;\n" +
                "    border: 1px solid #66afe9;\n" +
                "    border-radius: 3px;" +
                "    outline: 0;",
            iframe: {
                height: "34px",
                width: "100%"
            }
        },

        autoCardtypeDetection: {
            supportedCardtypes: supportedCardtypes,
            callback: function (detectedCardtype) {
                iframes.setCardType(detectedCardtype);
            }
        },

        events: {
            rendered: function () {
                $(".payone-loading").remove()
                $(".payone-form").removeClass("hidden")
            }
        },

        language: Payone.ClientApi.Language[$("#payone_lng").text()], //, // Language to display error-messages (default: Payone.ClientApi.Language.en)
        error: "payone_error" // area to display error-messages (optional)
    };

    request = JSON.parse($("#payone_req").html());
    var iframes = new Payone.ClientApi.HostedIFrames(config, request);

    $("#payone_other_card").click(
        function (e) {
            $("#payone_pseudocardpan").val("");
            $("#payone_truncatedcardpan").val("");
            $("#payone_cardtypeResponse").val("");
            $("#payone_cardexpiredateResponse").val("");
            $("#payone-current-card").slideUp();
            $(".payone-elements").slideDown();

            e.preventDefault();
            return false;
        }
    );

    if ($("#payone-current-card").length) {
        $(".payone-elements").hide();
    }

    window.payone_callback = function (response) {
        console.log(response)
        if (response.status !== "VALID") {
            $("#payone_error").append($("<div>").addClass("alert alert-danger").text(
                response.errormessage
            ));
            waitingDialog.hide();
        } else {
            document.getElementById("payone_pseudocardpan").value = response.pseudocardpan;
            document.getElementById("payone_truncatedcardpan").value = response.truncatedcardpan;
            document.getElementById("payone_cardtypeResponse").value = response.cardtype;
            document.getElementById("payone_cardexpiredateResponse").value = response.cardexpiredate;
            $('.payone-form').closest("form").get(0).submit();
        }
    }

    $('.payone-form').closest("form").submit(
        function () {
            $("#payone_error").html("");
            if (($("input[name=payment][value=payone_creditcard]").prop('checked') || $("input[name=payment][type=radio]").length === 0)
                && $("#payone_pseudocardpan").val() === "") {

                if ($("#payone_cardholder").val() !== "" && iframes.isComplete()) {
                    iframes.creditCardCheck('payone_callback');
                } else {
                    $("#payone_error").append($("<div>").addClass("alert alert-danger").text(
                        gettext("Please fill out all fields.")
                    ))
                    return false;
                }

                waitingDialog.show(gettext("Contacting payment provider …"));

                return false;
            }
        }
    );
});
