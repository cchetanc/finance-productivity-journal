import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend')))
from app import amadeus_client

class TestAmadeusClient(unittest.TestCase):
    @patch('app.amadeus_client.requests.post')
    def test_get_access_token(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "mock_token", "expires_in": 1800}
        mock_post.return_value = mock_response
        
        amadeus_client.AMADEUS_API_KEY = "test"
        amadeus_client.AMADEUS_API_SECRET = "test"
        amadeus_client._access_token = None
        amadeus_client._token_expiry = 0
        
        token = amadeus_client.get_access_token()
        self.assertEqual(token, "mock_token")
        
    @patch('app.amadeus_client.requests.get')
    @patch('app.amadeus_client.get_access_token')
    def test_get_hotel_availability(self, mock_get_token, mock_get):
        mock_get_token.return_value = "mock_token"
        
        def side_effect(url, *args, **kwargs):
            mock_resp = MagicMock()
            if url == amadeus_client.GEO_URL:
                mock_resp.json.return_value = {"data": [{"hotelId": "H1"}, {"hotelId": "H2"}]}
            elif url == amadeus_client.OFFERS_URL:
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "data": [
                        {
                            "hotel": {"hotelId": "H1", "name": "Test Hotel 1", "latitude": 48.8, "longitude": 2.3},
                            "offers": [
                                {"price": {"total": "100.00", "currency": "EUR"}}
                            ]
                        }
                    ]
                }
            return mock_resp
            
        mock_get.side_effect = side_effect
        
        # Clear cache
        amadeus_client._availability_cache.clear()
        
        results = amadeus_client.get_hotel_availability(48.8, 2.3, 5, "2023-12-01", "2023-12-05", 2)
        
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["hotel_id"], "H1")
        self.assertEqual(results[0]["lowest_rate"], "100.00")
        self.assertEqual(results[0]["available_rooms"], 1)

if __name__ == '__main__':
    unittest.main()
